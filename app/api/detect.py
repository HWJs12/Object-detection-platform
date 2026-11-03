"""检测相关接口: 图片检测 / 视频上传 / 视频实时检测 WebSocket。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, Form, UploadFile, WebSocket
from fastapi.concurrency import run_in_threadpool

from app.core.background_jobs import image_requests, video_jobs
from app.core.detection_service import detect_image_bytes
from app.core.provenance import file_sha256
from app.core.video_task import task_manager
from app.utils.exceptions import AppError, InferenceError
from app.utils.file_utils import release_upload, save_upload
from app.utils.response import ok
from app.db.stations import survey_for_station
from config import settings

# 业务接口统一 /api 前缀
router = APIRouter(prefix="/api", tags=["目标检测"])
# WebSocket 不使用 /api 前缀, 保持 /ws/... 短路径(前端按此连接)
ws_router = APIRouter(tags=["视频实时检测"])


def _read_and_detect(
    path: Path, original: str, conf: Optional[float], saved_name: str,
    survey: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """在线程池内一次性完成「读取字节 + 推理」。

    资源安全: 若在事件循环线程上调用 `path.read_bytes()`, 大图(数百 MB)会在事件
    循环线程把整个文件读进内存, 与解码后的数组共存导致峰值内存翻倍。此处把读取
    也放进线程池, 避免阻塞事件循环。
    """
    return detect_image_bytes(path.read_bytes(), original, conf, saved_name, survey)


@router.post("/detect/image", summary="图片目标检测")
async def api_detect_image(
    request_key: Optional[str] = Form(None, description="同一图片短期重试标识；服务重启或缓存过期后失效"),
    file: UploadFile = File(..., description="待检测的图片文件"),
    conf: Optional[float] = Form(None, description="置信度阈值, 留空使用全局配置"),
    site_id: int = Form(..., ge=1, description="地图中启用的调查站点编号，必填"),
    transect: Optional[str] = Form(None, description="样方 / 断面编号, 留空记为未标注样方"),
    depth_m: Optional[float] = Form(None, description="水深(米), 需为正数, 留空表示未记录"),
    survey_round: Optional[str] = Form(None, description="调查轮次, 如 2026春"),
) -> Dict[str, Any]:
    """上传图片 -> YOLO 推理 -> 返回绘制结果 / 统计 / 明细。"""
    # 输入校验: 与批量 / 视频接口保持一致, 非法置信度在入口拦截,
    # 避免越界值与负值穿透到推理层后以 500 内部错误回显。
    if conf is not None and not 0.01 <= float(conf) <= 0.99:
        raise AppError("置信度阈值需在 0.01 ~ 0.99 之间", code="INVALID_CONF")
    survey = survey_for_station(site_id, transect, depth_m, survey_round)
    path, original, _ = await save_upload(
        file, settings.upload_dir, settings.allowed_image_ext, settings.max_upload_mb, "图片"
    )
    async def perform():
        completed = False
        try:
            data = await run_in_threadpool(_read_and_detect, path, original, conf, path.name, survey)
            completed = True
            data['original_url'] = f'/data/uploads/{path.name}'
            return data
        except AppError:
            raise
        except Exception as exc:
            raise InferenceError(f'图片检测失败: {exc}') from exc
        finally:
            release_upload(path, discard_if_unreferenced=not completed)

    if isinstance(request_key, str) and request_key:
        owned = False
        try:
            if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request_key):
                raise AppError('图片重试标识格式错误', code='INVALID_REQUEST_KEY')
            digest = await run_in_threadpool(file_sha256, path)
            signature = hashlib.sha256(json.dumps({
                'hash': digest, 'name': original,
                'conf': settings.conf_threshold if conf is None else conf,
                # The station name is mutable; retry identity follows its stable ID.
                'survey': {key: value for key, value in survey.items() if key != 'site'},
            }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            future, owned = image_requests.claim(request_key, signature, perform)
        finally:
            if not owned:
                release_upload(path, discard_if_unreferenced=True)
        data = await asyncio.shield(future)
    else:
        data = await perform()
    return ok(data, f"检测完成, 共识别 {data['total']} 个目标")


@router.post("/detect/images", summary="批量图片目标检测")
async def api_detect_images(
    files: List[UploadFile] = File(..., description="待检测的图片文件, 可多选"),
    conf: Optional[float] = Form(None, description="置信度阈值, 留空使用全局配置"),
    site_id: int = Form(..., ge=1, description="地图中启用的调查站点编号，必填"),
    transect: Optional[str] = Form(None, description="样方 / 断面编号, 整批共用"),
    depth_m: Optional[float] = Form(None, description="水深(米), 需为正数, 留空表示未记录"),
    survey_round: Optional[str] = Form(None, description="调查轮次, 如 2026春"),
) -> Dict[str, Any]:
    """批量上传图片 -> 逐张推理并落库。

    单张失败不影响其它图片, 失败项在 items 中以 error 字段标识, 整体仍返回 200。
    调查维度(站点/样方/水深/轮次)整批共用: 一次批量通常对应同一站点的一次布设,
    逐张填写反而容易录错; 需要区分时按站点分批提交即可。
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

    survey = survey_for_station(site_id, transect, depth_m, survey_round)

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
            completed = False
            try:
                batch_bytes += int(size)
                data = await run_in_threadpool(
                    _read_and_detect, path, original, conf, path.name, survey
                )
                data["original_url"] = f"/data/uploads/{path.name}"
                data["error"] = None
                success += 1
                total_objects += int(data.get("total", 0))
                items.append(data)
                completed = True
            finally:
                release_upload(path, discard_if_unreferenced=not completed)
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
    site_id: int = Form(..., ge=1, description="地图中启用的调查站点编号，必填"),
    transect: Optional[str] = Form(None, description="样方 / 断面编号, 留空记为未标注样方"),
    depth_m: Optional[float] = Form(None, description="水深(米), 需为正数, 留空表示未记录"),
    survey_round: Optional[str] = Form(None, description="调查轮次, 如 2026春"),
) -> Dict[str, Any]:
    """上传视频 -> 探测元信息 -> 返回 task_id, 前端再连 WebSocket 开始逐帧检测。"""
    if conf is not None and not 0.01 <= float(conf) <= 0.99:
        raise AppError("置信度阈值需在 0.01 ~ 0.99 之间", code="INVALID_CONF")

    survey = survey_for_station(site_id, transect, depth_m, survey_round)
    path, original, _ = await save_upload(
        file, settings.upload_dir, settings.allowed_video_ext, settings.max_upload_mb,
        "视频", dedupe=False,
    )
    try:
        task = task_manager.create(path, original, conf, survey)
    except Exception:
        release_upload(path, discard_if_unreferenced=True)
        raise
    try:
        meta = await run_in_threadpool(task.probe)
    except AppError:
        await run_in_threadpool(task_manager.remove, task.task_id)
        raise
    except Exception as exc:
        await run_in_threadpool(task_manager.remove, task.task_id)
        raise AppError(f"视频解析失败: {exc}", code="VIDEO_OPEN_FAILED")

    video_jobs.start(task.task_id)
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
            "frame_skip": task.parameters["frame_skip"],
            "conf_threshold": task.conf_threshold,
            "device": task.parameters["device"],
        },
    }, "视频已上传，后台检测已开始")


@router.post("/detect/video/{task_id}/control", summary="控制视频检测任务(暂停/继续/停止/调阈值)")
async def api_control_video_task(
    task_id: str,
    payload: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    """对正在检测的视频任务下发控制指令。

    请求体:
        {"action": "pause"}                  暂停检测(保留当前播放位置)
        {"action": "resume"}                 继续检测
        {"action": "stop"}                   停止检测并释放跟踪资源
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
    elif action == "stop":
        task.cancel()
        # 上传后尚未接入 WebSocket 的任务没有 finally 清理路径, 需在此主动回收。
        if not task.running:
            await run_in_threadpool(task_manager.remove, task_id)
        message = "已停止检测，正在释放资源"
    else:
        raise AppError(
            "不支持的操作, 仅支持 pause / resume / stop / set_conf", code="INVALID_ACTION"
        )

    job = video_jobs.get(task_id)
    if job and not job.terminal:
        await job.publish({'type': 'task', 'task': job.snapshot()})
    return ok(task.status(), message)


@router.get('/detect/video/tasks', summary='后台视频任务及最近完成结果')
async def list_video_jobs() -> Dict[str, Any]:
    return ok({'items': video_jobs.snapshots()})


@router.get('/detect/video/{task_id}/status', summary='读取后台视频任务状态')
async def video_job_status(task_id: str) -> Dict[str, Any]:
    job = video_jobs.get(task_id)
    if job is None:
        raise AppError('任务已过期或服务已重启，请在检测记录中查看已完成结果',
                       code='TASK_NOT_FOUND', status_code=404)
    return ok(job.snapshot())


@ws_router.websocket('/ws/video/{task_id}')
async def ws_video_detect(websocket: WebSocket, task_id: str) -> None:
    """Subscribe to a job; disconnecting a viewer never cancels detection."""
    await websocket.accept()
    try:
        job = video_jobs.get(task_id) or video_jobs.start(task_id)
    except AppError as exc:
        await _safe_send(websocket, {'type': 'error', 'message': exc.message, 'code': exc.code})
        await _safe_close(websocket)
        return

    async def publish_updates():
        # Snapshot and sequence are captured without yielding: no terminal event is lost.
        sequence = job.sequence
        snapshot, frame, terminal = job.snapshot(), job.last_frame, job.terminal
        await websocket.send_json({'type': 'task', 'task': snapshot})
        if frame:
            await websocket.send_json(frame)
        if terminal:
            await websocket.send_json(terminal)
            return
        while True:
            try:
                sequence, event = await asyncio.wait_for(job.next_event(sequence), timeout=15)
            except asyncio.TimeoutError:
                await websocket.send_json({'type': 'task', 'task': job.snapshot()})
                continue
            await websocket.send_json(event)
            if event['type'] in ('done', 'error', 'stopped'):
                return

    async def receive_disconnect():
        while True:
            if (await websocket.receive())['type'] == 'websocket.disconnect':
                return

    sender = asyncio.create_task(publish_updates())
    receiver = asyncio.create_task(receive_disconnect())
    try:
        await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
    finally:
        sender.cancel()
        receiver.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
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
