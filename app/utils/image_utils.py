"""图像处理工具: 解码 / 绘制检测框 / 编码。

绘制使用 Pillow 而非 cv2.putText, 以便正确显示中文类别名称。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 常见中英文字体候选(按优先级)
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def get_font(size: int) -> ImageFont.FreeTypeFont:
    """按字号获取可用字体(带缓存), 全部缺失时回退默认字体。"""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font: Optional[ImageFont.FreeTypeFont] = None
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def class_color(class_id: int) -> Tuple[int, int, int]:
    """为类别生成稳定的 BGR 颜色。"""
    hue = (class_id * 47) % 180
    hsv = np.uint8([[[hue, 220, 240]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def imdecode(buf: bytes) -> Optional[np.ndarray]:
    """解码图片字节为 BGR 数组, 失败返回 None。"""
    if not buf:
        return None
    arr = np.frombuffer(buf, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def resize_long_side(img: np.ndarray, max_side: int) -> np.ndarray:
    """按比例缩放, 使长边不超过 max_side(0 表示不缩放)。"""
    if max_side <= 0:
        return img
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / longest
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def draw_detections(img: np.ndarray, detections: Sequence,
                    line_width: int = 2, font_scale: float = 0.55) -> np.ndarray:
    """在图像上绘制检测框与类别标签。

    Args:
        img: BGR 图像
        detections: 检测结果序列, 每项需含 class_id/class_name/confidence/bbox
        line_width: 框线宽度
        font_scale: 字体缩放系数(相对图像高度)
    """
    if img is None or not detections:
        return img

    h, _ = img.shape[:2]
    font_size = max(12, int(h * 0.028 * (font_scale / 0.55)))
    font = get_font(font_size)

    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    draw = ImageDraw.Draw(pil)

    for det in detections:
        x1, y1, x2, y2 = [int(v) for v in det.bbox]
        cid = int(getattr(det, "class_id", 0))
        name = str(getattr(det, "class_name", "obj"))
        conf = float(getattr(det, "confidence", 0.0))
        b, g, r = class_color(cid)  # BGR -> RGB 转换
        color = (r, g, b)

        draw.rectangle([x1, y1, x2, y2], outline=color, width=line_width)

        label = f"{name} {conf:.2f}"
        try:
            box = draw.textbbox((x1, y1), label, font=font)
            tw, th = box[2] - box[0], box[3] - box[1]
        except AttributeError:  # 兼容老版本 Pillow
            tw, th = draw.textsize(label, font=font)

        top = y1 - th - 6 if y1 - th - 6 > 0 else y1
        draw.rectangle([x1, top, x1 + tw + 8, top + th + 6], fill=color)
        draw.text((x1 + 4, top + 3), label, fill=(255, 255, 255), font=font)

    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def encode_jpeg(img: np.ndarray, quality: int = 85) -> bytes:
    """编码为 JPEG 字节。"""
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise ValueError("图像编码失败")
    return buf.tobytes()


def to_base64(img: np.ndarray, quality: int = 80) -> str:
    """编码为 base64 字符串(不含 data URI 前缀)。"""
    return base64.b64encode(encode_jpeg(img, quality)).decode("ascii")


def to_data_url(img: np.ndarray, quality: int = 85) -> str:
    """编码为可直接用于 <img src> 的 data URI。"""
    return "data:image/jpeg;base64," + to_base64(img, quality)


def save_image(img: np.ndarray, path: Path, quality: int = 90) -> Path:
    """保存图像到磁盘并返回路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]):
        raise OSError(f"图像保存失败: {path}")
    return path


def count_by_class(detections: Sequence) -> List[dict]:
    """按类别统计数量, 结果按数量降序。"""
    counter: dict[str, int] = {}
    for det in detections:
        name = str(getattr(det, "class_name", "obj"))
        counter[name] = counter.get(name, 0) + 1
    return [
        {"class_name": k, "count": v}
        for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
