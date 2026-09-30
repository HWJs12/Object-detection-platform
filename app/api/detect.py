"""检测相关接口: 图片检测 / 视频上传 / 视频实时检测 WebSocket。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from app.core.detection_service import detect_image_bytes
from app.core.video_task import task_manager
from app.utils.exceptions import AppError, InferenceError
from app.utils.file_utils import save_upload
from app.utils.response import fail, ok
from config import settings

# 业务接口统一 /api 前缀
router = APIRouter(prefix="/api", tags=["目标检测"])
# WebSocket 不使用 /api 前缀, 保持 /ws/... 短路径(前端按此连接)
ws_router = APIRouter(tags=["视频实时检测"])


@router.post("/detect/image", summary="图片目标检测")
async def api_detect_image(
    file: UploadFile = File(..., description="待检测的图片文件"),
    conf: Optional[float] = Form(None, description="置信度阈值, 留空使用全局配置"),
) -> Dict[str, Any]:
    """上传图片 -> YOLO 推理 -> 返回绘制结果 / 统计 / 明细。"""
    path, original, _ = await save_upload(
        file, settings.upload_dir, settings.allowed_image_ext, settings.max_upload_mb, "图片"
    )
    try:
        data = await run_in_threadpool(
            detect_image_bytes, path.read_bytes(), original, conf
        )
    except AppError:
        raise
    except Exception as exc:
        raise InferenceError(f"图片检测失败: {exc}")
    return ok(data, f"检测完成, 共识别 {data['total']} 个目标")


@router.post("/detect/video", summary="上传视频并创建检测任务")
async def api_upload_video(
    file: UploadFile = File(..., description="待检测的视频文件"),
    conf: Optional[float] = Form(None, description="置信度阈值, 留空使用全局配置"),
) -> Dict[str, Any]:
    """上传视频 -> 探测元信息 -> 返回 task_id, 前端再连 WebSocket 开始逐帧检测。"""
    if conf is not None and not 0.01 <= float(conf) <= 0.99:
        raise AppError("置信度阈值需在 0.01 ~ 0.99 之间", code="INVALID_CONF")

    path, original, _ = await save_upload(
        file, settings.upload_dir, settings.allowed_video_ext, settings.max_upload_mb, "视频"
    )
    task = task_manager.create(path, original, conf)
    try:
        meta = await run_in_threadpool(task.probe)
    except AppError:
        task_manager.remove(task.task_id)
        raise
    except Exception as exc:
        task_manager.remove(task.task_id)
        raise AppError(f"视频解析失败: {exc}", code="VIDEO_OPEN_FAILED")

    return ok({
        "task_id": task.task_id,
        "source_name": original,
        "meta": {
            "fps": round(meta.fps, 2),
            "total_frames": meta.total_frames,
            "width": meta.width,
            "height": meta.height,
            "duration_sec": meta.duration_sec,
        },
        "config": {
            "frame_skip": settings.frame_skip,
            "conf_threshold": task.conf_threshold,
            "device": settings.device_resolved,
        },
    }, "视频已上传, 正在等待开始检测")


@router.post("/detect/video/{task_id}/control", summary="控制视频检测任务(暂停/继续/调阈值)")
async def api_control_video_task(
    task_id: str,
    payload: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    """对正在检测的视频任务下发控制指令。

    请求体:
        {"action": "pause"}                  暂停检测(保留当前播放位置)
        {"action": "resume"}                 继续检测
        {"action": "set_conf", "value": 0.15} 运行中调整置信度阈值
    """
    task = task_manager.get(task_id)
    if task is None:
        raise AppError(
            "检测任务不存在或已结束, 请重新上传视频",
            code="TASK_NOT_FOUND",
            status_code=404,
        )

    action = str((payload or {}).get("action", "")).strip().lower()
    if action == "pause":
        task.pause()
        message = "已暂停检测"
    elif action == "resume":
        task.resume()
        message = "已继续检测"
    elif action == "set_conf":
        new_conf = task.set_conf((payload or {}).get("value"))
        message = f"置信度阈值已调整为 {new_conf}"
    else:
        raise AppError(
            "不支持的操作, 仅支持 pause / resume / set_conf", code="INVALID_ACTION"
        )

    return ok(task.status(), message)


@ws_router.websocket("/ws/video/{task_id}")
async def ws_video_detect(websocket: WebSocket, task_id: str) -> None:
    """视频逐帧检测实时推送。

    推送消息:
        {"type": "frame", "image": base64, "total": n, "counts": [...], ...}
        {"type": "done",  "summary": {...}}
        {"type": "error", "message": "..."}
    """
    await websocket.accept()
    task = task_manager.get(task_id)

    if task is None:
        await websocket.send_json({"type": "error", "message": "检测任务不存在或已过期, 请重新上传视频"})
        await websocket.close()
        return

    async def send(payload: Dict[str, Any]) -> None:
        await websocket.send_json(payload)

    try:
        await task.run(send)
    except WebSocketDisconnect:
        task.cancel()
    except AppError as exc:
        task.cancel()
        await _safe_send(websocket, {"type": "error", "message": exc.message, "code": exc.code})
    except Exception as exc:
        task.cancel()
        await _safe_send(websocket, {"type": "error", "message": f"视频检测失败: {exc}"})
    finally:
        task_manager.remove(task_id)
        await _safe_close(websocket)


async def _safe_send(websocket: WebSocket, payload: Dict[str, Any]) -> None:
    """尽力发送, 忽略连接已断开的情况。"""
    try:
        await websocket.send_json(payload)
    except Exception:
        pass


async def _safe_close(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except Exception:
        pass
