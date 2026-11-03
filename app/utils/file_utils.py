"""文件处理工具: 上传校验 / 安全落盘 / 路径生成。"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import UploadFile

from app.utils.exceptions import InvalidFileError

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
_CHUNK = 1024 * 1024  # 1MB 分片, 兼顾内存占用与 IO 效率
_ACTIVE_UPLOAD_LOCK = threading.Lock()
_ACTIVE_UPLOADS: dict[str, int] = {}


def _upload_key(path: Path) -> str:
    return str(path.resolve())


def _register_upload(path: Path) -> None:
    key = _upload_key(path)
    with _ACTIVE_UPLOAD_LOCK:
        _ACTIVE_UPLOADS[key] = _ACTIVE_UPLOADS.get(key, 0) + 1


def retain_file(path: Path) -> None:
    """保护正在生成、尚未入库的结果文件, 完成后调用 release_upload。"""
    _register_upload(path)


def _register_upload_locked(path: Path) -> None:
    key = _upload_key(path)
    _ACTIVE_UPLOADS[key] = _ACTIVE_UPLOADS.get(key, 0) + 1


def _release_upload_locked(path: Path) -> None:
    key = _upload_key(path)
    count = _ACTIVE_UPLOADS.get(key, 0)
    if count > 1:
        _ACTIVE_UPLOADS[key] = count - 1
    else:
        _ACTIVE_UPLOADS.pop(key, None)


def run_if_upload_unleased(path: Path, action):
    """持有租约锁执行一次清理动作, 防止上传登记与文件回收竞态。"""
    key = _upload_key(path)
    with _ACTIVE_UPLOAD_LOCK:
        if _ACTIVE_UPLOADS.get(key, 0):
            return False, None
        return True, action()


def active_upload_names() -> set[str]:
    """返回正在被接口使用的上传文件名, 清理任务用它保护未入库文件。"""
    with _ACTIVE_UPLOAD_LOCK:
        return {Path(key).name for key in _ACTIVE_UPLOADS}


def release_upload(path: Path, discard_if_unreferenced: bool = False) -> None:
    """释放一个上传文件租约; 失败任务只删除无活动租约且无数据库引用的文件。"""
    key = _upload_key(path)
    with _ACTIVE_UPLOAD_LOCK:
        count = _ACTIVE_UPLOADS.get(key, 0)
        if count > 1:
            _ACTIVE_UPLOADS[key] = count - 1
            return
        _ACTIVE_UPLOADS.pop(key, None)
        if not discard_if_unreferenced:
            return
        try:
            from app.core.storage import file_usage
            if file_usage(path.name) > 0:
                return
            path.unlink(missing_ok=True)
        except Exception:
            # 数据库或文件系统暂时不可用时保留孤儿, 交由存储治理回收。
            return


def safe_filename(filename: str) -> str:
    """清洗文件名, 防止路径穿越与非法字符。"""
    name = Path(filename or "").name
    name = _UNSAFE_CHARS.sub("_", name).strip().strip(".")
    return name or "unnamed"


def validate_extension(filename: str, allowed: tuple, kind: str = "文件") -> str:
    """校验扩展名是否在白名单内, 返回小写扩展名。"""
    ext = Path(filename or "").suffix.lower()
    if ext not in allowed:
        raise InvalidFileError(
            f"{kind}格式不支持: '{ext or '未知'}', 仅支持 {', '.join(allowed)}"
        )
    return ext


def validate_size(size: int, max_mb: int) -> None:
    """校验文件大小。"""
    if size <= 0:
        raise InvalidFileError("上传文件为空, 请重新选择文件")
    if size > max_mb * 1024 * 1024:
        raise InvalidFileError(f"文件过大: {size / 1024 / 1024:.1f}MB, 上限 {max_mb}MB")


def build_unique_path(dest_dir: Path, filename: str) -> Path:
    """生成不重复的目标路径: 时间戳 + 随机串 + 安全文件名。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = safe_filename(filename)
    return dest_dir / f"{stamp}_{uuid.uuid4().hex[:8]}_{safe}"


def _touch(path: Path) -> None:
    """刷新文件访问/修改时间(去重复用时调用, 避免被保留策略当作过期文件误删)。"""
    try:
        now = time.time()
        os.utime(path, (now, now))
    except OSError:
        pass


def _same_contents(left: Path, right: Path) -> bool:
    """逐块比较去重候选, 不仅依赖摘要或文件大小判断内容相同。"""
    try:
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                ca, cb = a.read(_CHUNK), b.read(_CHUNK)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


async def save_upload(file: UploadFile, dest_dir: Path, allowed: tuple,
                      max_mb: int, kind: str = "文件",
                      dedupe: Optional[bool] = None) -> tuple[Path, str, int]:
    """流式保存上传文件, 返回 (保存路径, 原始文件名, 字节数)。

    去重(P1-4): 按文件内容 SHA-256 命名。内容相同的重复上传直接复用已有文件并刷新
    mtime, 避免每次检测都留下一份完整副本导致磁盘无限膨胀。

    实现上先写入 .tmp 隐藏文件, 算完摘要后再落位 —— 既能边读边算摘要(不必把
    整个文件读进内存), 又能在发现重复时干净地丢弃临时副本。

    Raises:
        InvalidFileError: 文件为空 / 类型不支持 / 超过大小上限
    """
    if dedupe is None:
        dedupe = bool(settings_dedupe_enabled())

    original = safe_filename(file.filename or "")
    ext = validate_extension(original, allowed, kind)

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".tmp_{uuid.uuid4().hex}"
    max_bytes = max_mb * 1024 * 1024

    # 上传写入期间也登记租约, 避免运维清理把正在写入的 .tmp 文件当作残留删除。
    _register_upload(tmp)

    digest = hashlib.sha256()
    total = 0
    try:
        with open(tmp, "wb") as fp:
            while chunk := await file.read(_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise InvalidFileError(f"文件过大, 上限为 {max_mb}MB")
                digest.update(chunk)
                fp.write(chunk)
    except InvalidFileError:
        tmp.unlink(missing_ok=True)
        release_upload(tmp)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        release_upload(tmp)
        raise InvalidFileError(f"文件保存失败: {exc}")
    except BaseException:
        # 包括请求取消; 取消时也要释放临时租约, 避免临时文件被永久标为活动。
        tmp.unlink(missing_ok=True)
        release_upload(tmp)
        raise

    if total <= 0:
        tmp.unlink(missing_ok=True)
        release_upload(tmp)
        raise InvalidFileError("上传文件为空, 请重新选择文件")

    try:
        if dedupe:
            desired = dest_dir / f"{digest.hexdigest()}{ext}"
        else:
            # 未开启去重: 时间戳 + 随机串命名, 保证同名文件互不覆盖
            desired = build_unique_path(dest_dir, original)
        dest = _publish_upload(tmp, desired, total, dedupe)
        return dest, original, total
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InvalidFileError(f"文件保存失败: {exc}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        # _publish_upload 会在锁内完成租约转移; 失败路径在这里释放临时租约。
        release_upload(tmp)


def _publish_upload(tmp: Path, desired: Path, total: int, dedupe: bool) -> Path:
    """在清理锁内复用或发布文件, 并原子转移活动租约。"""
    with _ACTIVE_UPLOAD_LOCK:
        dest = desired
        if dedupe and dest.exists() and dest.stat().st_size == total and _same_contents(tmp, dest):
            _register_upload_locked(dest)
            tmp.unlink(missing_ok=True)
            _release_upload_locked(tmp)
            _touch(dest)
            return dest
        if dest.exists():
            # 摘要路径冲突或极低概率随机名冲突时, 绝不覆盖已有数据。
            suffix = uuid.uuid4().hex[:12]
            dest = dest.with_name(f"{dest.stem}_{suffix}{dest.suffix}")
            while dest.exists():
                suffix = uuid.uuid4().hex[:12]
                dest = desired.with_name(f"{desired.stem}_{suffix}{desired.suffix}")
        _register_upload_locked(dest)
        try:
            tmp.replace(dest)
        except OSError:
            _release_upload_locked(dest)
            raise
        _release_upload_locked(tmp)
        return dest


def settings_dedupe_enabled() -> bool:
    """读取全局去重开关(延迟导入, 避免模块循环依赖)。"""
    try:
        from config import settings
        return bool(settings.dedupe_upload)
    except Exception:
        return False


def human_size(num_bytes: int) -> str:
    """字节数转人类可读字符串。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"
