"""检测相关接口: 图片检测 / 视频上传 / 视频实时检测 WebSocket。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _read_and_detect(
    path: Path, original: str, conf: Optional[float], saved_name: str
) -> Dict[str, Any]:
    """在线程池内一次性完成「读取字节 + 推理」。

    资源安全: 若在事件循环线程上调用 `path.read_bytes()`, 大图(数百 MB)会在事件
    循环线程把整个文件读进内存, 与解码后的数组共存导致峰值内存翻倍。此处把读取
    也放进线程池, 避免阻塞事件循环。
    """
    return detect_image_bytes(path.read_bytes(), original, conf, saved_name)


@router.post("/detect/image", summary="图片目标检测")
async def api_detect_image(
    file: UploadFile = File(..., description="待检测的图片文件"),
    conf: Optional[float] = Form(None, description="置信度阈值, 留空使用全局配置"),
) -> Dict[str, Any]:
    """上传图片 -> YOLO 推理 -> 返回绘制结果 / 统计 / 明细。"""
    # 输入校验: 与批量 / 视频接口保持一致, 非法置信度在入口拦截,
    # 避免越界值与负值穿透到推理层后以 500 内部错误回显。
    if conf is not None and not 0.01 <= float(conf) <= 0.99:
        raise AppError("置信度阈值需在 0.01 ~ 0.99 之间", code="INVALID_CONF")
    path, original, _ = await save_upload(
        file, settings.upload_dir, settings.allowed_image_ext, settings.max_upload_mb, "图片"
    )
    try:
        data = await run_in_threadpool(
            _read_and_detect, path, original, conf, path.name
        )
    except AppError:
        raise
    except Exception as exc:
        raise InferenceError(f"图片检测失败: {exc}")
    # 原图地址: 供前端用 Canvas 叠加检测框(支持类别筛选)
    data["original_url"] = f"/data/uploads/{path.name}"
    return ok(data, f"检测完成, 共识别 {data['total']} 个目标")


@router.post("/detect/images", summary="批量图片目标检测")
async def api_detect_images(
    files: List[UploadFile] = File(..., description="待检测的图片文件, 可多选"),
    conf: Optional[float] = Form(None, description="置信度阈值, 留空使用全局配置"),
) -> Dict[str, Any]:
    """批量上传图片 -> 逐张推理并落库。

    单张失败不影响其它图片, 失败项在 items 中以 error 字段标识, 整体仍返回 200。
    """
    if not files:
        raise AppError("请至少上传一张图片", code="NO_FILE")
    if len(files) > settings.max_batch_images:
        raise AppError(
            f"单次最多检测 {settings.max_batch_images} 张图片, 当前 {len(files)} 张",
            code="TOO_MANY_FILES",
        )
    if conf is not None and not 0.01 <= float(conf) <= 0.99:
        raise AppError("置信度阈值需在 0.01 ~ 0.99 之间", code="INVALID_CONF")

    items: List[Dict[str, Any]] = []
    success = 0
    total_objects = 0
    # 安全: 单次批量累计大小上限, 防止 20 张 × 300MB 之类的组合写满磁盘。
    # 按单文件上限的 3 倍作为整批上限(与"单张失败不阻断整批"语义一致)。
    batch_max_bytes = settings.max_upload_mb * 3 * 1024 * 1024
    batch_bytes = 0
    for idx, file in enumerate(files):
        name = file.filename or f"image_{idx + 1}"
        if batch_bytes >= batch_max_bytes:
            items.append({
                "source_name": name, "total": 0, "counts": [],
                "detections": [],
                "error": (
                    f"批量累计超限: 本批已处理 {batch_bytes / 1024 / 1024:.1f}MB, "
                    f"超过整批上限 {batch_max_bytes / 1024 / 1024:.0f}MB, 已停止后续检测"
                ),
            })
            continue
        try:
            path, original, size = await save_upload(
                file, settings.upload_dir, settings.allowed_image_ext,
                settings.max_upload_mb, "图片",
            )
            batch_bytes += int(size)
            data = await run_in_threadpool(
                _read_and_detect, path, original, conf, path.name
            )
            data["original_url"] = f"/data/uploads/{path.name}"
            data["error"] = None
            success += 1
            total_objects += int(data.get("total", 0))
            items.append(data)
        except AppError as exc:
            items.append({
                "source_name": name, "total": 0, "counts": [],
                "detections": [], "error": exc.message,
            })
        except Exception as exc:  # 单张异常不阻断整批
            items.append({
                "source_name": name, "total": 0, "counts": [],
                "detections": [], "error": f"检测失败: {exc}",
            })

    return ok({
        "items": items,
        "total_images": len(files),
        "success_count": success,
        "fail_count": len(files) - success,
        "total_objects": total_objects,
        "conf_threshold": float(conf) if conf is not None else settings.conf_threshold,
    }, f"批量检测完成: 成功 {success} 张, 失败 {len(files) - success} 张")


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
        await run_in_threadpool(task_manager.remove, task.task_id)
        raise
    except Exception as exc:
        await run_in_threadpool(task_manager.remove, task.task_id)
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
        # shutdown(wait=True) 会阻塞至 worker 结束, 必须在线程池中调用以免卡死事件循环。
        await run_in_threadpool(task_manager.remove, task_id)
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
