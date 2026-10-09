"""存储治理: 过期记录清理 / 孤儿回收 / 去重治理 / 临时文件清理。

职责边界
--------
本模块负责**何时清理**(策略与调度); 具体"文件能否删"的判定(引用计数、
回收站、审计)统一由 `app.core.storage` 提供, 避免两处各写一套导致行为不一致。

设计要点:
    - **引用计数安全**: 去重机制下同一文件可能被多条记录共享, 删除前必须确认
      已无任何引用, 否则会误删其他记录仍在使用的数据。
    - **判断依据是引用关系, 不是文件时间**: 旧实现用 mtime + 保留期判断孤儿,
      导致"刚删记录留下的文件要等满保留期才清"。现在二者解耦。
    - **回收站兜底**: 被回收的文件先进 `data/.trash/`, 超过窗口才彻底删除。
    - **安全边界**: 只处理 uploads / results 两个受管目录, 一律用 resolve() 校验,
      不做任何目录级递归删除。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List

from app.core.storage import (
    cleanup_orphans,
    purge_trash,
    referenced_names,
    release_record_files,
    storage_audit,
    trash_stats,
)
from app.db import dao
from app.db.database import checkpoint, get_connection
from config import settings

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # 摘要计算时的读取分片, 避免大文件读入内存


def _unlink(target: Path | None) -> int:
    """删除单个文件, 返回实际释放的字节数。"""
    if target is None or not target.exists():
        return 0
    try:
        size = target.stat().st_size
        target.unlink()
        return size
    except OSError as exc:
        logger.debug("文件删除失败 %s: %s", target.name, exc)
        return 0


def cleanup_expired(retention_days: int | None = None) -> Dict[str, Any]:
    """清理超过保留天数的检测记录与其文件。

    注意: **孤儿文件的回收与保留期无关** —— 只要无记录引用即回收(见
    `cleanup_orphans`)。保留期只约束"有记录但太久没动"的数据, 二者解耦。

    Args:
        retention_days: 保留天数, 覆盖配置项; <=0 表示不清理记录本身。
    """
    days = settings.file_retention_days if retention_days is None else int(retention_days)
    days = int(days)

    files = 0
    freed = 0

    # ---- 1) 过期记录: 按保留期删除记录 + 其独占文件 ----
    if days > 0:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        expired = get_connection().execute(
            "SELECT id, saved_name, result_image, result_video FROM detections "
            "WHERE created_at < ?",
            (cutoff,),
        ).fetchall()

        for row in expired:
            record = dict(row)
            dao.delete_record(row["id"])
            # 引用计数: 共享文件不删, 独占文件移入回收站
            freed += release_record_files(record, to_trash=True)["freed_bytes"]
            files += 1
    else:
        logger.info("记录保留策略已关闭(file_retention_days=%s), 仅回收孤儿文件", days)

    # ---- 2) 孤儿文件: 与保留期解耦, 只要无引用即回收 ----
    orphan = cleanup_orphans(dry_run=False, to_trash=True, min_age_sec=60)
    freed += int(orphan["freed_bytes"])

    if files or orphan["count"]:
        logger.info(
            "存储清理: 删除记录 %d 条, 回收文件 %d 个 / %.1f MB (记录保留期 %s 天)",
            files, len(orphan["files"]), freed / 1024 / 1024,
            days if days > 0 else "不限",
        )
        checkpoint()

    return {
        "records": files,
        "files": files,
        "orphans": orphan["count"],
        "freed_bytes": freed,
        "skipped": False,
    }


def _digest_of(path: Path, limit: int = 512 * 1024 * 1024) -> str | None:
    """计算文件内容 MD5(流式, 不把整个文件读进内存)。"""
    try:
        if path.stat().st_size > limit:
            return None
        digest = hashlib.md5()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def cleanup_duplicates(dry_run: bool = False) -> Dict[str, Any]:
    """清理内容重复的历史文件(治理存量, 去重机制本身只能防新增)。

    规则: 每种内容最多保留一份 —— 优先保留"被数据库记录引用"的那份, 未被任何
    记录引用的多余副本一律删除。被调用的系统在能力上是幂等的, 重复执行无副作用。

    Args:
        dry_run: True 时只统计不删除, 用于预检。

    Returns:
        {"candidates": [(文件名, 大小)], "freed_bytes": n, "count": n}
    """
    referenced = referenced_names()

    keep_digests: set[str] = set()
    orphan_digests: set[str] = set()
    candidates: List[tuple[Path, int]] = []

    for directory in (settings.upload_dir, settings.result_dir):
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith(".tmp_"):
                continue
            name = path.name
            if name in referenced:
                d = _digest_of(path)
                if d:
                    keep_digests.add(d)      # 被引用的文件内容必须保留
                continue
            d = _digest_of(path)
            if not d:
                continue
            if d in keep_digests or d in orphan_digests:
                candidates.append((path, path.stat().st_size))
            else:
                orphan_digests.add(d)        # 每种孤儿内容保留第一份

    freed = 0
    removed: List[str] = []
    for path, size in candidates:
        if dry_run:
            freed += size
            removed.append(path.name)
            continue
        freed += _unlink(path)
        removed.append(path.name)

    if candidates and not dry_run:
        logger.info("重复文件清理: 删除 %d 个副本, 回收 %.1f MB",
                    len(candidates), freed / 1024 / 1024)
        checkpoint()

    return {"count": len(candidates), "freed_bytes": freed, "files": removed}


def purge_tmp_files() -> int:
    """清理上传中断残留的 .tmp_ 临时文件与 0 字节残骸, 返回清理数量。

    除了 `.tmp_` 前缀(上传中途中断留下的过渡文件), 还处理"已落位但内容为空"
    的残骸 —— 上传在 rename 之后、数据写满之前被中断时会留下这种文件。
    """
    count = 0
    for directory in (settings.upload_dir, settings.result_dir):
        if not directory.exists():
            continue
        for path in directory.glob(".tmp_*"):
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
        # 0 字节且无记录引用的文件: 必然是中断残骸, 安全清理
        referenced = referenced_names()
        for path in directory.iterdir():
            if not path.is_file() or path.name in referenced:
                continue
            try:
                if path.stat().st_size == 0:
                    path.unlink()
                    count += 1
            except OSError:
                pass
    if count:
        logger.info("临时文件清理: 删除 %d 个残留文件", count)
    return count


def storage_stats() -> Dict[str, Any]:
    """受管目录存储占用概况(供 health 接口展示)。

    在基础占用之上额外给出**孤儿占用**与**回收站占用**, 便于一眼看出
    "有多少空间是被无引用的文件白白占着的"。
    """
    audit = storage_audit()
    return {
        "uploads": {"bytes": audit["uploads"]["bytes"], "files": audit["uploads"]["files"]},
        "results": {"bytes": audit["results"]["bytes"], "files": audit["results"]["files"]},
        "total_bytes": audit["total_bytes"],
        "orphan_bytes": audit["orphan_bytes"],
        "orphan_files": audit["orphan_files"],
        "trash": audit["trash"],
        "retention_days": settings.file_retention_days,
    }


def storage_detail() -> Dict[str, Any]:
    """完整存储审计明细(供存储治理接口使用)。"""
    return {
        **storage_audit(),
        "retention_days": settings.file_retention_days,
        "trash_bytes": trash_stats()["bytes"],
    }
