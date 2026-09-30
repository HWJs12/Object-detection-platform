"""文件处理工具: 上传校验 / 安全落盘 / 路径生成。"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.utils.exceptions import InvalidFileError

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


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


async def save_upload(file: UploadFile, dest_dir: Path, allowed: tuple,
                      max_mb: int, kind: str = "文件") -> tuple[Path, str, int]:
    """流式保存上传文件, 返回 (保存路径, 原始文件名, 字节数)。

    Raises:
        InvalidFileError: 文件为空 / 类型不支持 / 超过大小上限
    """
    original = safe_filename(file.filename or "")
    validate_extension(original, allowed, kind)

    dest = build_unique_path(dest_dir, original)
    max_bytes = max_mb * 1024 * 1024
    total = 0
    try:
        with open(dest, "wb") as fp:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    fp.close()
                    dest.unlink(missing_ok=True)
                    raise InvalidFileError(f"文件过大, 上限为 {max_mb}MB")
                fp.write(chunk)
    except InvalidFileError:
        raise
    except OSError as exc:
        raise InvalidFileError(f"文件保存失败: {exc}")

    if total <= 0:
        dest.unlink(missing_ok=True)
        raise InvalidFileError("上传文件为空, 请重新选择文件")

    return dest, original, total


def human_size(num_bytes: int) -> str:
    """字节数转人类可读字符串。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"
