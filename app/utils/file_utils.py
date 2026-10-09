"""文件处理工具: 上传校验 / 安全落盘 / 路径生成。"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import UploadFile

from app.utils.exceptions import InvalidFileError

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
_CHUNK = 1024 * 1024  # 1MB 分片, 兼顾内存占用与 IO 效率


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


async def save_upload(file: UploadFile, dest_dir: Path, allowed: tuple,
                      max_mb: int, kind: str = "文件",
                      dedupe: Optional[bool] = None) -> tuple[Path, str, int]:
    """流式保存上传文件, 返回 (保存路径, 原始文件名, 字节数)。

    去重(P1-4): 按文件内容 MD5 命名。内容相同的重复上传直接复用已有文件并刷新
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

    digest = hashlib.md5()
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
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InvalidFileError(f"文件保存失败: {exc}")

    if total <= 0:
        tmp.unlink(missing_ok=True)
        raise InvalidFileError("上传文件为空, 请重新选择文件")

    if dedupe:
        dest = dest_dir / f"{digest.hexdigest()[:16]}{ext}"
        if dest.exists() and dest.stat().st_size == total:
            tmp.unlink(missing_ok=True)  # 已有完全相同的内容, 丢弃本次临时副本
            _touch(dest)
            return dest, original, total
        try:
            tmp.replace(dest)
        except OSError:
            tmp.rename(dest)
        return dest, original, total

    # 未开启去重: 保持时间戳 + 随机串命名, 保证同名文件互不覆盖
    dest = build_unique_path(dest_dir, original)
    tmp.replace(dest)
    return dest, original, total


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
