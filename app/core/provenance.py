"""记录推理时的模型与参数, 不将密钥或外部服务配置写入检测记录。"""
from __future__ import annotations

import datetime as dt
import hashlib
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from config import settings

_hash_cache: dict[tuple[str, int, int], str] = {}
_hash_lock = threading.Lock()


def file_sha256(path: Path) -> str:
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _hash_lock:
        cached = _hash_cache.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    with _hash_lock:
        if len(_hash_cache) > 128:
            _hash_cache.clear()
        _hash_cache[key] = value
    return value


def inference_snapshot(parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """白名单快照; 调用者可传入引擎实际加载版本与任务冻结参数。"""
    overrides = dict(parameters or {})
    if "model" in overrides:
        model = overrides.pop("model")
    else:
        path = settings.model_path_abs
        model: Dict[str, Any] = {"name": path.name if path else None, "sha256": None}
        if path and path.exists():
            model.update(sha256=file_sha256(path), bytes=path.stat().st_size)
    snapshot: Dict[str, Any] = {
        "app_version": "1.0.0",
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "device": settings.device_resolved,
        "parameters": {key: getattr(settings, key) for key in (
            "conf_threshold", "iou_threshold", "imgsz", "max_det", "frame_skip",
            "video_max_side", "tracker", "track_enabled", "jpeg_quality", "push_fps_limit",
        )},
    }
    if "device" in overrides:
        snapshot["device"] = overrides.pop("device")
    snapshot["parameters"].update(overrides)
    return snapshot
