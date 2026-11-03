"""模型与运行时配置接口。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool

from app.core.yolo_engine import engine
from app.utils.exceptions import AppError
from app.utils.response import ok
from config import settings

router = APIRouter(prefix="/api", tags=["模型与配置"])

# 允许运行时调整的字段及其取值范围
_UPDATABLE = {
    "conf_threshold": (0.01, 0.99),
    "iou_threshold": (0.01, 0.99),
    "imgsz": (320, 1920),
    "frame_skip": (1, 10),
    "video_max_side": (320, 1920),
    "jpeg_quality": (30, 100),
    "push_fps_limit": (0.0, 60.0),
}


@router.get("/model/info", summary="模型信息")
async def model_info() -> Dict[str, Any]:
    """返回模型路径 / 设备 / 类别列表(类别名取自模型自身)。"""
    return ok(engine.info())


@router.post("/model/reload", summary="重新加载模型")
async def reload_model() -> Dict[str, Any]:
    """更换权重文件后调用, 强制重新加载。"""
    try:
        await run_in_threadpool(engine.reload)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(f"模型重新加载失败: {exc}", code="MODEL_LOAD_FAILED", status_code=500)
    return ok(engine.info(), "模型已重新加载")


@router.get("/config", summary="获取运行时配置")
async def get_config() -> Dict[str, Any]:
    return ok(settings.to_dict())


@router.put("/config", summary="更新运行时配置")
async def update_config(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Validate the complete update before applying it; active videos keep their snapshot."""
    pending = {}
    integer_fields = {"imgsz", "frame_skip", "video_max_side", "jpeg_quality"}
    for key, value in (payload or {}).items():
        if key not in _UPDATABLE:
            continue
        low, high = _UPDATABLE[key]
        try:
            if isinstance(value, bool):
                raise ValueError("boolean is not a numeric configuration value")
            number = float(value)
        except (TypeError, ValueError):
            raise AppError(f"配置项 {key} 必须是数字", code="INVALID_CONFIG")
        if not (low <= number <= high):
            raise AppError(f"配置项 {key} 需在 {low} ~ {high} 之间", code="INVALID_CONFIG")
        if key in integer_fields and not number.is_integer():
            raise AppError(f"配置项 {key} 必须是整数", code="INVALID_CONFIG")
        pending[key] = int(number) if key in integer_fields else number

    if not pending:
        raise AppError(f"没有可更新的配置项, 支持: {', '.join(_UPDATABLE)}", code="INVALID_CONFIG")

    # No await between validation and commit: other requests see all-or-nothing.
    for key, value in pending.items():
        setattr(settings, key, value)
    return ok(settings.to_dict(), f"配置已更新: {', '.join(pending)}；运行中的视频保持创建时的参数")
