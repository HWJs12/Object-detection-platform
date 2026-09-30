"""图片检测业务: 解码 -> 推理 -> 绘制 -> 统计 -> 落库。"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.core.yolo_engine import Detection, engine
from app.db import dao
from app.utils.exceptions import InferenceError, InvalidFileError
from app.utils.image_utils import count_by_class, draw_detections, imdecode, save_image
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
                       conf: Optional[float] = None) -> Dict[str, Any]:
    """执行图片检测全流程。

    Args:
        image_bytes: 图片原始字节
        source_name: 原始文件名
        conf: 置信度阈值, 为空时使用全局配置

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
    started = time.perf_counter()
    detections = engine.predict(image, conf=used_conf)
    infer_ms = (time.perf_counter() - started) * 1000

    # 2) 绘制检测框并保存结果图
    drawn = draw_detections(image.copy(), detections)
    height, width = drawn.shape[:2]
    result_name = f"result_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    result_path = settings.result_dir / result_name
    try:
        save_image(drawn, result_path)
        result_url = _result_url(result_path)
    except OSError as exc:
        raise InferenceError(f"结果图保存失败: {exc}")

    # 3) 统计
    counts = count_by_class(detections)
    class_stats = _class_stats(detections)

    # 4) 落库
    created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record_id = dao.insert_detection({
        "task_type": "image",
        "source_name": source_name,
        "saved_name": result_name,
        "result_image": result_path.name,
        "created_at": created_at,
        "total_objects": len(detections),
        "image_width": width,
        "image_height": height,
        "inference_ms": round(infer_ms, 1),
        "conf_threshold": used_conf,
        "status": "success",
    })
    dao.insert_items(record_id, [d.to_dict() for d in detections])
    dao.insert_class_stats(record_id, class_stats)

    return {
        "record_id": record_id,
        "source_name": source_name,
        "created_at": created_at,
        "result_url": result_url,
        "width": width,
        "height": height,
        "total": len(detections),
        "counts": counts,
        "detections": [d.to_dict() for d in detections],
        "inference_ms": round(infer_ms, 1),
        "conf_threshold": used_conf,
        "device": settings.device_resolved,
    }
