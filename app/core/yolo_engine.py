"""YOLO 推理引擎封装。

职责:
    - 懒加载并按路径缓存模型(避免每次请求重复加载)
    - 统一推理入口, 输出结构化的 Detection 列表
    - 类别名称一律从模型自身读取(model.names), 绝不写死

线程安全: 推理加锁, 避免多请求并发争抢模型资源。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from app.utils.exceptions import InferenceError, ModelLoadError, ModelNotConfiguredError
from config import settings


@dataclass
class Detection:
    """单个检测目标。"""

    class_id: int
    class_name: str
    confidence: float
    bbox: List[float]  # [x1, y1, x2, y2]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": round(float(self.confidence), 4),
            "bbox": [round(float(v), 2) for v in self.bbox],
        }


class YoloEngine:
    """YOLO 模型单例封装。"""

    def __init__(self) -> None:
        self._model = None
        self._model_path: Optional[Path] = None
        self._names: Dict[int, str] = {}
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    # ---------------- 模型加载 ----------------
    def load(self, force: bool = False):
        """加载模型(带缓存)。force=True 时强制重新加载。"""
        with self._load_lock:
            path = settings.model_path_abs
            if not path:
                raise ModelNotConfiguredError(
                    "未找到模型权重文件, 请将训练好的 .pt 文件放入 models/ 目录, "
                    "或通过 MODEL_PATH 环境变量指定路径"
                )
            if not path.exists():
                raise ModelNotConfiguredError(f"模型权重文件不存在: {path}")

            if self._model is not None and not force and self._model_path == path:
                return self._model

            try:
                from ultralytics import YOLO
                model = YOLO(str(path))
                self._names = dict(model.names) if hasattr(model, "names") else {}
                self._model = model
                self._model_path = path
                self._warmup(model)
            except ModelLoadError:
                raise
            except Exception as exc:  # 加载阶段任何异常都转成可读信息
                self._model = None
                raise ModelLoadError(f"模型加载失败: {exc}")
        return self._model

    def _warmup(self, model) -> None:
        """预跑一张空图, 消除首次推理的额外开销。"""
        try:
            dummy = np.zeros((settings.imgsz, settings.imgsz, 3), dtype=np.uint8)
            model.predict(
                source=dummy,
                conf=settings.conf_threshold,
                iou=settings.iou_threshold,
                imgsz=settings.imgsz,
                device=settings.device_resolved,
                verbose=False,
            )
        except Exception:
            # warmup 失败不影响正常推理, 静默忽略
            pass

    # ---------------- 推理 ----------------
    def predict(self, image: np.ndarray, conf: Optional[float] = None,
                iou: Optional[float] = None, imgsz: Optional[int] = None) -> List[Detection]:
        """对单张 BGR 图像推理, 返回 Detection 列表。"""
        if image is None or getattr(image, "size", 0) == 0:
            raise InferenceError("图像内容为空, 无法推理")

        model = self.load()
        conf = settings.conf_threshold if conf is None else conf
        iou = settings.iou_threshold if iou is None else iou
        imgsz = settings.imgsz if imgsz is None else imgsz

        try:
            with self._infer_lock:  # 串行推理, 保证 CPU 环境下的稳定性
                results = model.predict(
                    source=image,
                    conf=conf,
                    iou=iou,
                    imgsz=imgsz,
                    device=settings.device_resolved,
                    max_det=settings.max_det,
                    verbose=False,
                )
        except Exception as exc:
            raise InferenceError(f"模型推理失败: {exc}")

        return self._parse(results)

    def _parse(self, results) -> List[Detection]:
        """解析 ultralytics 结果为 Detection 列表(类别名取自模型)。"""
        detections: List[Detection] = []
        if not results:
            return detections

        result = results[0]
        names = getattr(result, "names", None) or self._names or {}
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return detections

        xyxy = boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, "cpu") else np.asarray(boxes.xyxy)
        confs = boxes.conf.cpu().numpy() if hasattr(boxes.conf, "cpu") else np.asarray(boxes.conf)
        clses = boxes.cls.cpu().numpy() if hasattr(boxes.cls, "cpu") else np.asarray(boxes.cls)

        name_map = settings.class_name_map or {}
        for (x1, y1, x2, y2), conf, cls in zip(xyxy, confs, clses):
            cid = int(cls)
            raw_name = str(names.get(cid, f"class_{cid}"))
            detections.append(
                Detection(
                    class_id=cid,
                    class_name=str(name_map.get(raw_name, raw_name)),
                    confidence=float(conf),
                    bbox=[float(x1), float(y1), float(x2), float(y2)],
                )
            )
        return detections

    # ---------------- 元信息 ----------------
    @property
    def names(self) -> Dict[int, str]:
        """当前模型的类别名称映射(应用显示名映射之后)。"""
        if self._names:
            return self.display_names
        try:
            self.load()
        except Exception:
            return {}
        return self.display_names

    @property
    def display_names(self) -> Dict[int, str]:
        """应用可选显示名映射(class_name_map)后的类别名。"""
        name_map = settings.class_name_map or {}
        return {cid: str(name_map.get(name, name)) for cid, name in self._names.items()}

    def info(self) -> Dict[str, Any]:
        """模型信息(供前端 /api/model/info 展示)。"""
        path = settings.model_path_abs
        loaded = self._model is not None
        classes: List[Dict[str, Any]] = []
        if loaded:
            classes = [{"id": cid, "name": name} for cid, name in sorted(self.display_names.items())]
        return {
            "model_path": path.as_posix() if path else None,
            "model_name": path.name if path else None,
            "model_exists": bool(path and path.exists()),
            "loaded": loaded,
            "device": settings.device_resolved,
            "imgsz": settings.imgsz,
            "conf_threshold": settings.conf_threshold,
            "iou_threshold": settings.iou_threshold,
            "classes": classes,
            "class_count": len(classes),
        }


# 全局引擎单例
engine = YoloEngine()
