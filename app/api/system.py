"""系统状态与运维接口: 运行时快照 / 存储治理 / 回收站管理。

设计要点:
    - **聚合快照**: `/api/system/status` 一次返回前端运维面板所需的全部数据,
      避免多个请求之间数值不同步(例如存储统计与任务数来自不同时刻)。
    - **危险操作显式确认**: 彻底删除类操作要求显式传参, 清空回收站默认只清
      "已过滞留窗口"的文件, 不会一键抹掉刚删没多久、还可能想恢复的数据。
    - **写入动作走线程池**: 文件遍历与删除是阻塞 IO, 不能占住事件循环。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from app.utils.exceptions import AppError
from app.utils.response import ok

router = APIRouter(prefix="/api/system", tags=["系统状态"])


def _has_video_tasks() -> int:
    from app.core.video_task import task_manager

    return len(task_manager)


def _guard_no_video_tasks(action: str) -> None:
    """视频任务运行期间禁止动文件, 否则会删掉任务正在读写的文件。"""
    count = _has_video_tasks()
    if count > 0:
        raise AppError(
            f"当前有 {count} 个视频任务在队列中, {action}会导致任务文件被删除, 请等待结束后重试",
            code="SYSTEM_BUSY",
            status_code=409,
        )


@router.get("/status", summary="系统状态快照")
async def system_status() -> Dict[str, Any]:
    """一次返回进程 / 主机 / 存储 / 模型 / 任务 / 数据规模全部指标。"""
    from app.core.sysinfo import snapshot

    return ok(await run_in_threadpool(snapshot))


@router.get("/audit-log", summary="存储治理审计日志")
async def audit_log(limit: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """最近的清理 / 回收 / 恢复记录(来自 data/cleanup.log)。"""
    from app.core.sysinfo import recent_audit

    return ok({"items": await run_in_threadpool(lambda: recent_audit(limit))})


@router.post("/checkpoint", summary="WAL 手动 checkpoint")
async def checkpoint_now() -> Dict[str, Any]:
    """把 WAL 内容合并回主库并截断 WAL 文件, 释放其占用。"""
    from app.core.storage import checkpoint_db

    await run_in_threadpool(checkpoint_db)
    return ok(None, "数据库 WAL 已合并并截断")


@router.post("/stale-tasks/reap", summary="回收僵尸视频任务")
async def reap_tasks() -> Dict[str, Any]:
    """立即回收超过 TTL 且未开始检测的任务, 并删除其上传文件。"""
    from app.core.video_task import task_manager

    count = await run_in_threadpool(task_manager.reap)
    return ok({"reaped": count}, f"已回收 {count} 个滞留任务" if count else "没有需要回收的任务")


@router.post("/orphans/cleanup", summary="回收孤儿文件")
async def clean_orphans(
    dry_run: bool = Query(True, description="默认只预检, 传 false 才真正回收"),
    min_age_sec: int = Query(60, ge=0, le=86400, description="跳过最近修改过的文件(秒)"),
) -> Dict[str, Any]:
    """回收无任何记录引用的文件(与保留期无关)。"""
    from app.core.storage import cleanup_orphans

    result = await run_in_threadpool(
        lambda: cleanup_orphans(dry_run=dry_run, to_trash=True, min_age_sec=min_age_sec)
    )
    freed = result["freed_bytes"]
    return ok(
        {**result, "freed_mb": round(freed / 1024 / 1024, 2), "dry_run": dry_run},
        f"{'预检: 可' if dry_run else '已'}回收 {result['count']} 个孤儿文件, "
        f"{freed / 1024 / 1024:.2f} MB",
    )


@router.post("/duplicates/cleanup", summary="清理重复副本")
async def clean_duplicates(
    dry_run: bool = Query(True, description="默认只预检, 传 false 才真正删除"),
) -> Dict[str, Any]:
    """清理内容重复的历史文件(去重机制只能防新增, 存量需本接口治理)。"""
    from app.core.cleaner import cleanup_duplicates

    result = await run_in_threadpool(lambda: cleanup_duplicates(dry_run=dry_run))
    freed = result["freed_bytes"]
    return ok(
        {**result, "freed_mb": round(freed / 1024 / 1024, 2), "dry_run": dry_run},
        f"{'预检: 可' if dry_run else '已'}清理 {result['count']} 个重复副本, "
        f"{freed / 1024 / 1024:.2f} MB",
    )


@router.post("/tmp/cleanup", summary="清理临时残留")
async def clean_tmp() -> Dict[str, Any]:
    """清理上传中断留下的 .tmp_ 文件与 0 字节残骸。"""
    from app.core.cleaner import purge_tmp_files

    count = await run_in_threadpool(purge_tmp_files)
    return ok({"count": count}, f"已清理 {count} 个残留文件" if count else "没有残留文件")


@router.get("/trash", summary="回收站文件列表")
async def trash_list(limit: int = Query(50, ge=1, le=500)) -> Dict[str, Any]:
    """回收站概况 + 文件明细(按删除时间倒序), 供面板展示与单个恢复。"""
    from app.core.storage import original_name_of, trash_dir, trash_stats

    stats = await run_in_threadpool(trash_stats)

    def _list() -> list:
        import datetime as _dt
        import time as _time

        now_ts = _time.time()
        items = []
        for p in trash_dir().iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "trash_name": p.name,
                "original_name": original_name_of(p.name),
                "bytes": st.st_size,
                "age_days": round((now_ts - st.st_mtime) / 86400, 2),
                "deleted_at": _dt.datetime.fromtimestamp(
                    st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        items.sort(key=lambda x: x["age_days"])
        return items[:limit]

    files = await run_in_threadpool(_list)
    return ok({**stats, "files_detail": files})


@router.post("/trash/purge", summary="清空回收站")
async def purge_trash_api(
    days: int = Query(7, ge=0, le=365, description="仅清除滞留超过该天数的文件; 0 表示全部"),
    confirm: bool = Query(False, description="days=0 时必须显式确认"),
) -> Dict[str, Any]:
    """彻底删除回收站文件, 真正释放磁盘。"""
    from app.core.storage import purge_trash

    if days == 0 and not confirm:
        raise AppError(
            "清空全部回收站内容不可恢复, 需显式确认(confirm=true)",
            code="CONFIRM_REQUIRED",
            status_code=400,
        )
    _guard_no_video_tasks("清空回收站")

    result = await run_in_threadpool(lambda: purge_trash(older_than_days=days))
    return ok(
        {**result, "freed_mb": round(result["freed_bytes"] / 1024 / 1024, 2)},
        f"回收站已清理 {result['count']} 个文件, 释放 {result['freed_bytes'] / 1024 / 1024:.2f} MB",
    )


@router.post("/trash/restore", summary="恢复回收站文件")
async def restore_trash_api(
    name: str = Query(..., description="回收站中的文件名(trash_name)"),
) -> Dict[str, Any]:
    """把误删的文件从回收站恢复到原目录。"""
    from app.core.storage import restore_from_trash

    result = await run_in_threadpool(lambda: restore_from_trash(name))
    if not result:
        raise AppError("回收站中未找到该文件", code="TRASH_NOT_FOUND", status_code=404)
    return ok(result, f"已恢复: {result['restored']}")


@router.post("/cleanup", summary="一键综合清理")
async def full_cleanup(
    days: int = Query(0, ge=0, le=3650, description="记录保留天数, 0 表示按配置值"),
    duplicates: bool = Query(True, description="是否一并清理重复副本"),
    dry_run: bool = Query(False, description="只统计不删除, 用于预检"),
) -> Dict[str, Any]:
    """过期记录 + 重复副本 + 临时残留 一次性清理。"""
    from app.core.cleaner import cleanup_duplicates, cleanup_expired, purge_tmp_files

    _guard_no_video_tasks("执行清理")

    result = await run_in_threadpool(
        lambda: {
            "expired": cleanup_expired(days or None),
            "duplicates": cleanup_duplicates(dry_run=dry_run) if duplicates else {"count": 0},
        }
    )
    tmp_removed = 0 if dry_run else await run_in_threadpool(purge_tmp_files)

    freed = result["expired"].get("freed_bytes", 0) + result["duplicates"].get("freed_bytes", 0)
    return ok(
        {
            **result,
            "tmp_removed": tmp_removed,
            "freed_bytes": freed,
            "freed_mb": round(freed / 1024 / 1024, 2),
            "dry_run": dry_run,
        },
        f"清理完成{' (预检, 未实际删除)' if dry_run else ''}, 可回收 {freed / 1024 / 1024:.1f} MB",
    )


@router.get("/config-summary", summary="关键配置摘要")
async def config_summary() -> Dict[str, Any]:
    """把与运维相关的配置项单独列出, 便于面板显示当前策略。"""
    from config import settings

    return ok({
        "retention_days": settings.file_retention_days,
        "auto_cleanup": settings.auto_cleanup,
        "dedupe_upload": settings.dedupe_upload,
        "max_active_video_tasks": settings.max_active_video_tasks,
        "video_task_ttl_sec": settings.video_task_ttl_sec,
        "trash_purge_days": 7,
        "max_upload_mb": settings.max_upload_mb,
        "max_batch_images": settings.max_batch_images,
    })
