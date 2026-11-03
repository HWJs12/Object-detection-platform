"""图片检测业务: 解码 -> 推理 -> 绘制 -> 统计 -> 落库。"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.core.yolo_engine import Detection, engine
from app.core.provenance import inference_snapshot
from app.db import dao
from app.utils.exceptions import InferenceError, InvalidFileError
from app.utils.file_utils import release_upload, retain_file
from app.utils.image_utils import count_by_class, draw_detections, imdecode, save_image
from app.utils.survey import normalize_survey
from config import settings


def _class_stats(detections: List[Detection]) -> List[Tuple[int, str, int]]:
    """按类别汇总 -> [(class_id, class_name, count)]。"""
    counter: Dict[int, Dict[str, Any]] = {}
    for det in detections:
        item = counter.setdefault(
            det.class_id, {"class_id": det.class_id, "class_name": det.class_name, "count": 0}
        )
        item["count"] += 1
    return [(v["class_id"], v["class_name"], v["count"]) for v in counter.values()]


def _result_url(path: Path) -> str:
    """结果文件相对 URL(对应 /data 静态挂载)。"""
    try:
        rel = path.relative_to(settings.data_dir)
    except ValueError:
        rel = Path(path.name)
    return "/data/" + rel.as_posix()


def detect_image_bytes(image_bytes: bytes, source_name: str,
                       conf: Optional[float] = None,
                       saved_name: Optional[str] = None,
                       survey: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行图片检测全流程。

    Args:
        image_bytes: 图片原始字节
        source_name: 原始文件名(用户视角)
        conf: 置信度阈值, 为空时使用全局配置
        saved_name: 服务器磁盘上的源图文件名。入库后该文件受清理器保护,
            否则源图会变成"无记录引用"的孤儿文件被保留策略误删。
        survey: 调查维度(站点/样方/水深/调查轮次), 仅入库, 不参与推理。

    Returns:
        检测结果字典(含结果图 URL / 统计 / 明细)

    Raises:
        InvalidFileError: 图片为空或无法解码
        InferenceError: 模型推理失败
    """
    if not image_bytes:
        raise InvalidFileError("上传的图片内容为空")

    image: np.ndarray | None = imdecode(image_bytes)
    if image is None:
        raise InvalidFileError("图片无法解析, 文件可能已损坏或格式不受支持")

    # 1) 推理并计时
    used_conf = settings.conf_threshold if conf is None else float(conf)
    parameters = {
        "conf_threshold": used_conf, "iou_threshold": settings.iou_threshold,
        "imgsz": settings.imgsz, "max_det": settings.max_det,
        "device": settings.device_resolved,
    }
    started = time.perf_counter()
    detections, model_metadata = engine.predict_with_provenance(
        image, conf=used_conf, iou=parameters["iou_threshold"],
        imgsz=parameters["imgsz"], max_det=parameters["max_det"], device=parameters["device"],
    )
    infer_ms = (time.perf_counter() - started) * 1000
    snapshot = inference_snapshot({**parameters, "model": model_metadata})
    source_hash = hashlib.sha256(image_bytes).hexdigest()

    # 2) 绘制检测框并保存结果图
    drawn = draw_detections(image.copy(), detections)
    counts = count_by_class(detections)
    class_stats = _class_stats(detections)
    height, width = drawn.shape[:2]
    result_name = f"result_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    result_path = settings.result_dir / result_name
    retain_file(result_path)
    completed = False
    try:
        save_image(drawn, result_path)
        result_url = _result_url(result_path)
    except Exception as exc:
        release_upload(result_path, discard_if_unreferenced=True)
        raise InferenceError(f"结果图保存失败: {exc}")

    # 4) 落库
    created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = {
        "task_type": "image",
        "source_name": source_name,
        "saved_name": saved_name or "",      # 磁盘上的源图文件名(去重后的名字)
        "result_image": result_path.name,    # 带检测框的结果图文件名
        "created_at": created_at,
        "total_objects": len(detections),
        "unique_objects": len(detections),
        "total_detections": len(detections),
        "image_width": width,
        "image_height": height,
        "inference_ms": round(infer_ms, 1),
        "conf_threshold": used_conf,
        "status": "success",
        "counting_mode": "image_objects",
        "unique_count_valid": True,
        "source_hash": source_hash,
        "inference_snapshot": snapshot,
        # 调查维度: 图片的"单帧峰值"即本图检出数, 两条链路口径保持一致
        **normalize_survey(survey),
        "maxn_per_class": {name: int(cnt) for _cid, name, cnt in class_stats},
        "maxn_total": len(detections),
    }
    try:
        record_id = dao.save_detection(record, [d.to_dict() for d in detections], class_stats)
        completed = True
    finally:
        release_upload(result_path, discard_if_unreferenced=not completed)

    return {
        "record_id": record_id,
        "source_name": source_name,
        "created_at": created_at,
        "result_url": result_url,
        "width": width,
        "height": height,
        "total": len(detections),
        "unique_objects": len(detections),
        "counting_mode": "image_objects",
        "unique_count_valid": True,
        "source_hash": source_hash,
        "inference_snapshot": snapshot,
        "counts": counts,
        "detections": [d.to_dict() for d in detections],
        "inference_ms": round(infer_ms, 1),
        "conf_threshold": used_conf,
        "device": settings.device_resolved,
    }
