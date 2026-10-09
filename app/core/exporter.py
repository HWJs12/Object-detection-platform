"""检测结果标签导出: CSV / COCO JSON / YOLO TXT(zip)。

设计为纯函数模块: 输入是 api 层已组装好的 items(与 PDF 报告同源), 输出 bytes,
不触碰数据库与磁盘, 便于独立验证与复用。

items 每项结构:
    {
        "source_name": "xxx.jpg",     # 原始文件名
        "width": 720, "height": 405,  # 原图尺寸(YOLO 归一化必需)
        "detections": [
            {"class_id": 0, "class_name": "hd", "confidence": 0.93,
             "bbox": [x1, y1, x2, y2]},   # 绝对像素, 左上/右下
        ],
    }
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 格式 -> (扩展名, MIME)
FORMATS: Dict[str, Tuple[str, str]] = {
    "csv": (".csv", "text/csv; charset=utf-8"),
    "coco": (".json", "application/json; charset=utf-8"),
    "yolo": (".zip", "application/zip"),
}

CSV_HEADER: List[str] = [
    "image", "class_id", "class_name", "confidence",
    "x1", "y1", "x2", "y2",
    "img_width", "img_height",
    "cx", "cy", "w", "h",
]


# ---------------------------------------------------------------- 内部工具

def _collect_categories(items: Sequence[Dict[str, Any]]) -> List[Tuple[int, str]]:
    """收集 class_id -> class_name, 按 class_id 升序。

    统一从 same 数据源取名, 保证 CSV / COCO / YOLO 三种格式的类别映射完全一致。
    """
    mapping: Dict[int, str] = {}
    for item in items:
        for det in item.get("detections") or []:
            cid = det.get("class_id")
            name = det.get("class_name")
            if cid is None or name is None:
                continue
            mapping.setdefault(int(cid), str(name))
    return sorted(mapping.items())


def _norm_box(x1: float, y1: float, x2: float, y2: float,
              width: Any, height: Any) -> Optional[Tuple[float, float, float, float]]:
    """换算为 YOLO 归一化 (cx, cy, w, h); 无法换算时返回 None。

    跳过情形: 宽高缺失/非法、框退化(零宽高)、坐标为负。
    """
    try:
        w_img = float(width)
        h_img = float(height)
    except (TypeError, ValueError):
        return None
    if w_img <= 0 or h_img <= 0:
        return None
    if x2 <= x1 or y2 <= y1:
        return None

    cx = (x1 + x2) / 2.0 / w_img
    cy = (y1 + y2) / 2.0 / h_img
    bw = (x2 - x1) / w_img
    bh = (y2 - y1) / h_img
    # 检测框可能因缩放略微越界, 收敛到 [0, 1] 避免下游训练报错
    clamp = lambda v: max(0.0, min(1.0, v))
    return clamp(cx), clamp(cy), clamp(bw), clamp(bh)


def _parse_bbox(det: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """取出合法 bbox, 非法(缺字段/含 None/退化)返回 None。"""
    bbox = det.get("bbox") or []
    if len(bbox) != 4 or any(v is None for v in bbox):
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _round(value: Any, digits: int) -> Any:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return ""


def _image_name(item: Dict[str, Any]) -> str:
    return str(item.get("source_name") or item.get("saved_name") or "unknown")


# ---------------------------------------------------------------- CSV

def build_csv(items: Sequence[Dict[str, Any]]) -> bytes:
    """一行一个目标框。归一化列(cx/cy/w/h)便于直接接入训练脚本。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)

    for item in items:
        name = _image_name(item)
        width, height = item.get("width"), item.get("height")
        for det in item.get("detections") or []:
            box = _parse_bbox(det)
            if box is None:
                continue
            x1, y1, x2, y2 = box
            norm = _norm_box(x1, y1, x2, y2, width, height)
            writer.writerow([
                name,
                det.get("class_id", ""),
                det.get("class_name", ""),
                _round(det.get("confidence"), 4),
                round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2),
                width if width else "",
                height if height else "",
                *([_round(v, 6) for v in norm] if norm else ["", "", "", ""]),
            ])
    # utf-8-sig: 带 BOM, Excel 双击打开不乱码
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------- COCO

def build_coco(items: Sequence[Dict[str, Any]]) -> bytes:
    """标准 COCO detection 结构。bbox 为绝对像素 [x, y, w, h]。

    category_id 采用 1-based(class_id + 1), 符合 COCO 规范(0 位保留)。
    无目标的图片仍写入 images(作为负样本), 只是没有对应 annotations。
    """
    categories = _collect_categories(items)

    images: List[Dict[str, Any]] = []
    annotations: List[Dict[str, Any]] = []
    ann_id = 1

    for img_id, item in enumerate(items, start=1):
        width, height = item.get("width"), item.get("height")
        images.append({
            "id": img_id,
            "file_name": _image_name(item),
            "width": int(width) if width else 0,
            "height": int(height) if height else 0,
        })
        for det in item.get("detections") or []:
            box = _parse_bbox(det)
            if box is None:
                continue
            x1, y1, x2, y2 = box
            bw, bh = x2 - x1, y2 - y1
            cid = det.get("class_id")
            annotations.append({
                "id": ann_id,
                "image_id": img_id,
                "category_id": int(cid) + 1 if cid is not None else 0,
                "bbox": [round(x1, 2), round(y1, 2), round(bw, 2), round(bh, 2)],
                "area": round(bw * bh, 2),
                "iscrowd": 0,
                "score": _round(det.get("confidence"), 4),
            })
            ann_id += 1

    payload = {
        "info": {
            "description": "水下海洋生物目标检测数据集",
            "source": "基于 GSCNet 的海洋检测平台",
        },
        "licenses": [],
        "images": images,
        "annotations": annotations,
        "categories": [
            {"id": cid + 1, "name": name, "supercategory": "object"}
            for cid, name in categories
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------- YOLO

def _unique_stem(stem: str, used: Dict[str, int]) -> str:
    """同名图片(重复上传)时追加序号, 避免标签文件互相覆盖。"""
    if stem not in used:
        used[stem] = 1
        return stem
    used[stem] += 1
    return f"{stem}_{used[stem]}"


def _build_readme(categories: Sequence[Tuple[int, str]]) -> str:
    names = ", ".join(f"'{name}'" for _, name in categories)
    class_lines = "\n".join(f"  {cid} = {name}" for cid, name in categories)
    return f"""YOLO 格式标签导出
==================

目录结构:
  labels/<图片名>.txt   每张图对应一个标签文件(无目标则为空文件, 表示负样本)
  classes.txt           类别列表, 行号即 class_id
  README.txt            本说明

标签格式(每行一个目标):
  <class_id> <cx> <cy> <w> <h>
  坐标已归一化到 0~1, 保留 6 位小数

类别顺序:
{class_lines}

配套使用:
  把同名图片放入 images/, 标签放在 labels/, 即可用于 Ultralytics 训练。
  data.yaml 示例:
    path: .
    train: images
    val: images
    nc: {len(categories)}
    names: [{names}]
"""


def build_yolo(items: Sequence[Dict[str, Any]]) -> bytes:
    """打包 zip: labels/*.txt + classes.txt + README.txt。"""
    categories = _collect_categories(items)
    used: Dict[str, int] = {}

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            stem = _unique_stem(Path(_image_name(item)).stem or "image", used)
            lines: List[str] = []
            width, height = item.get("width"), item.get("height")
            for det in item.get("detections") or []:
                box = _parse_bbox(det)
                if box is None:
                    continue
                norm = _norm_box(*box, width, height)
                if norm is None:
                    continue
                cid = det.get("class_id")
                cx, cy, bw, bh = norm
                lines.append(
                    f"{int(cid) if cid is not None else 0} "
                    f"{cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"
                )
            # 空文件即负样本, 符合 YOLO 约定
            zf.writestr(f"labels/{stem}.txt", "\n".join(lines) + ("\n" if lines else ""))

        zf.writestr("classes.txt", "\n".join(name for _, name in categories) + "\n")
        zf.writestr("README.txt", _build_readme(categories))

    return buffer.getvalue()


# ---------------------------------------------------------------- 分发

_BUILDERS = {"csv": build_csv, "coco": build_coco, "yolo": build_yolo}


def export_labels(items: Sequence[Dict[str, Any]], fmt: str) -> Tuple[bytes, str, str]:
    """按格式导出, 返回 (文件内容, 扩展名, MIME)。格式非法时抛 ValueError。"""
    key = (fmt or "").strip().lower()
    if key not in _BUILDERS:
        raise ValueError(f"不支持的导出格式: {fmt}")
    content = _BUILDERS[key](items)
    ext, media_type = FORMATS[key]
    return content, ext, media_type
