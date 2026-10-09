"""系统运行时状态采集: 进程指标 / 存储水位 / 任务状态 / 数据规模。

设计要点:
    - **零硬依赖**: 进程 CPU / 内存优先取 `psutil`; 未安装时降级为
      "仅自身进程可用信息" + `resource` 兜底, 保证接口在任何环境都能返回,
      不会因为缺一个可选依赖而整页报错。
    - **只读**: 本模块不做任何清理或修改动作, 采集失败一律降级为 None,
      由前端显示"—", 不影响其它字段。
    - **聚合一次返回**: 运维面板需要多个维度的数据, 拆成多个接口会让前端
      发 6~7 个请求且数值不同步, 因此聚合为一个快照。
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from typing import Any, Dict, Optional

from config import BASE_DIR, settings

# 进程启动时刻, 用于计算运行时长
_STARTED_AT = time.time()

try:  # psutil 为可选依赖, 缺失时功能降级但不报错
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except Exception:  # pragma: no cover - 环境相关
    psutil = None  # type: ignore
    _HAS_PSUTIL = False


def _process_info() -> Dict[str, Any]:
    """当前进程的 CPU / 内存占用(psutil 缺失时降级)。"""
    info: Dict[str, Any] = {
        "pid": os.getpid(),
        "uptime_sec": round(time.time() - _STARTED_AT, 1),
        "psutil": _HAS_PSUTIL,
        "cpu_percent": None,
        "memory_mb": None,
        "memory_percent": None,
        "threads": None,
    }
    if not _HAS_PSUTIL:
        return info
    try:
        proc = psutil.Process(os.getpid())
        with proc.oneshot():
            info["cpu_percent"] = round(proc.cpu_percent(interval=None), 1)
            mem = proc.memory_info()
            info["memory_mb"] = round(mem.rss / 1024 / 1024, 1)
            info["memory_percent"] = round(proc.memory_percent(), 2)
            info["threads"] = proc.num_threads()
    except Exception:
        pass
    return info


def _system_info() -> Dict[str, Any]:
    """主机 CPU / 内存 / 磁盘水位。"""
    info: Dict[str, Any] = {
        "cpu_count": os.cpu_count(),
        "cpu_percent": None,
        "memory_total_mb": None,
        "memory_used_mb": None,
        "memory_percent": None,
        "disk_total_gb": None,
        "disk_used_gb": None,
        "disk_free_gb": None,
        "disk_percent": None,
    }
    if _HAS_PSUTIL:
        try:
            info["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            info["memory_total_mb"] = round(vm.total / 1024 / 1024)
            info["memory_used_mb"] = round((vm.total - vm.available) / 1024 / 1024)
            info["memory_percent"] = vm.percent
        except Exception:
            pass

    # 磁盘: 用 shutil 即可, 不依赖 psutil
    try:
        usage = shutil.disk_usage(str(settings.data_dir))
        info["disk_total_gb"] = round(usage.total / 1024 ** 3, 2)
        info["disk_used_gb"] = round((usage.total - usage.free) / 1024 ** 3, 2)
        info["disk_free_gb"] = round(usage.free / 1024 ** 3, 2)
        info["disk_percent"] = round((usage.total - usage.free) / usage.total * 100, 1)
    except Exception:
        pass
    return info


def _db_size() -> Dict[str, Any]:
    """数据库文件与 WAL 体积。"""
    out = {"db_bytes": 0, "wal_bytes": 0, "total_bytes": 0}
    for suffix, key in (("", "db_bytes"), ("-wal", "wal_bytes")):
        path = settings.db_path.parent / (settings.db_path.name + suffix)
        try:
            if path.exists():
                out[key] = path.stat().st_size
        except OSError:
            pass
    out["total_bytes"] = out["db_bytes"] + out["wal_bytes"]
    return out


def _data_scale() -> Dict[str, Any]:
    """数据规模: 记录数 / 图片视频数 / 检测目标累计。"""
    from app.db import dao

    out: Dict[str, Any] = {
        "records": 0, "image_records": 0, "video_records": 0,
        "objects": 0, "unique_objects": 0, "items": 0,
    }
    try:
        out.update(dao.stats_overview())
    except Exception:
        pass

    # 明细表总行数(展示"检测框累计数")
    try:
        from app.db.database import get_connection

        out["items"] = int(
            get_connection().execute("SELECT COUNT(*) AS c FROM detection_items").fetchone()["c"]
        )
    except Exception:
        pass
    return out


def _video_tasks() -> Dict[str, Any]:
    """驻留视频任务概览(不返回单个任务的内部对象, 只给统计)。"""
    from app.core.video_task import task_manager

    tasks = []
    try:
        for task in list(getattr(task_manager, "_tasks", {}).values()):
            tasks.append({
                "task_id": task.task_id,
                "source_name": getattr(task, "source_name", None),
                "running": bool(getattr(task, "running", False)),
                "processed": int(getattr(task, "processed", 0) or 0),
                "total_frames": int(getattr(task, "total_frames", 0) or 0),
                "idle_sec": round(time.time() - float(getattr(task, "created_at", time.time())), 1),
            })
    except Exception:
        pass

    return {
        "count": len(tasks),
        "limit": settings.max_active_video_tasks,
        "ttl_sec": settings.video_task_ttl_sec,
        "running": sum(1 for t in tasks if t["running"]),
        "tasks": tasks,
    }


def snapshot() -> Dict[str, Any]:
    """聚合一次完整的状态快照(供运维面板单次拉取)。"""
    from app.core.cleaner import storage_detail

    def _safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    storage = _safe(storage_detail, {}) or {}
    engine_info = _safe(lambda: __import__("app.core.yolo_engine", fromlist=["engine"]).engine.info(), {}) or {}

    uploads = storage.get("uploads", {}) or {}
    results = storage.get("results", {}) or {}
    trash = storage.get("trash", {}) or {}

    managed_bytes = int(uploads.get("bytes", 0)) + int(results.get("bytes", 0))
    orphan_bytes = int(storage.get("orphan_bytes", 0))
    trash_bytes = int(trash.get("bytes", 0))

    return {
        "server_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "process": _process_info(),
        "system": _system_info(),
        "database": _db_size(),
        "storage": {
            "uploads": uploads,
            "results": results,
            "trash": trash,
            "managed_bytes": managed_bytes,
            "orphan_bytes": orphan_bytes,
            "orphan_files": int(storage.get("orphan_files", 0)),
            "trash_bytes": trash_bytes,
            "total_bytes": int(storage.get("total_bytes", 0)),
            "retention_days": settings.file_retention_days,
            "reclaimable_bytes": orphan_bytes + trash_bytes,
        },
        "model": {
            "name": engine_info.get("model_name"),
            "loaded": engine_info.get("loaded", False),
            "device": engine_info.get("device") or settings.device_resolved,
            "class_count": engine_info.get("class_count", 0),
            "imgsz": engine_info.get("imgsz"),
            "conf_threshold": engine_info.get("conf_threshold"),
            "tracker": settings.tracker,
            "track_enabled": settings.track_enabled,
        },
        "video_tasks": _video_tasks(),
        "tracking_busy": _safe(lambda: __import__("app.core.yolo_engine", fromlist=["engine"]).engine.track_busy(), False),
        "data_scale": _data_scale(),
        "paths": {
            "base_dir": str(BASE_DIR),
            "data_dir": str(settings.data_dir),
            "db_path": str(settings.db_path),
        },
    }


def recent_audit(limit: int = 20) -> list:
    """读取最近的存储治理审计记录(cleanup.log 尾部)。"""
    log = settings.data_dir / "cleanup.log"
    if not log.exists():
        return []
    try:
        with open(log, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
    except OSError:
        return []

    import json

    out = []
    for line in reversed(lines[-limit * 2:]):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out
