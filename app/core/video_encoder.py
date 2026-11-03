"""Write browser-compatible H.264 MP4 without downloading an encoder at runtime."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np


def ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise RuntimeError("没有可用的 H.264 编码器，请安装 imageio-ffmpeg 后重新检测") from exc
    if not Path(executable).is_file():
        raise RuntimeError("H.264 编码器不存在，请重新安装 imageio-ffmpeg")
    return executable


class H264VideoWriter:
    """Fixed-size BGR frames to H.264/yuv420p MP4, including odd-size padding."""

    def __init__(self, path: Path, fps: float, size: tuple[int, int]):
        self.size = size
        self._closed = False
        executable = ffmpeg_executable()
        self._stderr = tempfile.TemporaryFile()
        width, height = size
        args = [
            executable, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr24",
            "-video_size", f"{width}x{height}", "-framerate", str(fps),
            "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
        ]
        try:
            self._process = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except BaseException:
            self._stderr.close()
            raise

    def write(self, frame: np.ndarray) -> None:
        if self._closed:
            raise RuntimeError("视频编码器已关闭")
        if (frame.shape[1], frame.shape[0]) != self.size:
            raise ValueError("视频帧尺寸改变，已停止导出以避免生成不完整视频")
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("视频编码器只接受三通道 uint8 BGR 图像")
        self._process.stdin.write(np.ascontiguousarray(frame).tobytes())

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            try:
                self._process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                return_code = self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
                raise RuntimeError("H.264 视频编码超时")
            if return_code:
                self._stderr.seek(0)
                detail = self._stderr.read().decode("utf-8", errors="replace")[-1500:]
                raise RuntimeError(f"H.264 视频编码失败: {detail}")
        finally:
            self._stderr.close()
