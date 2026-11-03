"""按历史检测明细浏览目标；视频明细为检测时保存的采样帧。"""

from __future__ import annotations

import json
import math
import threading
from functools import lru_cache
from pathlib import Path
from typing import Literal

import cv2
from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.core.storage import resolve_record_file
from app.db.database import get_connection
from app.utils.exceptions import AppError
from app.utils.image_utils import MAX_DECODE_PIXELS, imdecode, resize_long_side
from app.utils.response import ok

router = APIRouter(prefix="/api/gallery", tags=["目标图库"])
_thumbnail_slots = threading.BoundedSemaphore(3)


@router.get("", summary="分页浏览检测目标")
def list_gallery(
    category: str = Query("", max_length=100),
    source: Literal["", "image", "video"] = "",
    keyword: str = Query("", max_length=100),
    min_confidence: float = Query(0, ge=0, le=1),
    order: Literal["recent", "confidence"] = "recent",
    page: int = Query(1, ge=1, le=1000000),
    page_size: int = Query(24, ge=1, le=60),
):
    conn = get_connection()
    conditions = ["d.status = 'success'", "d.task_type IN ('image', 'video')"]
    args: list = []
    if source:
        conditions.append("d.task_type = ?")
        args.append(source)
    if keyword.strip():
        conditions.append("instr(lower(d.source_name), lower(?)) > 0")
        args.append(keyword.strip())
    conditions.append("i.confidence >= ?")
    args.append(min_confidence)
    base_where = " AND ".join(conditions)
    join = " FROM detection_items i JOIN detections d ON d.id = i.detection_id "
    categories = [dict(r) for r in conn.execute(
        "SELECT i.class_name, COUNT(*) AS count" + join + " WHERE " + base_where
        + " GROUP BY i.class_name ORDER BY count DESC, i.class_name ASC", args
    )]
    if category:
        conditions.append("i.class_name = ?")
        args.append(category)
    where = " AND ".join(conditions)
    total = conn.execute("SELECT COUNT(*)" + join + " WHERE " + where, args).fetchone()[0]
    ordering = ("i.confidence DESC, i.id DESC" if order == "confidence"
                else "d.created_at DESC, d.id DESC, i.confidence DESC, i.id ASC")
    rows = conn.execute(
        "SELECT i.id, i.detection_id, i.class_name, i.confidence, i.frame_index, "
        "d.task_type, d.source_name, d.created_at, d.site" + join
        + " WHERE " + where + " ORDER BY " + ordering + " LIMIT ? OFFSET ?",
        [*args, page_size, (page - 1) * page_size],
    ).fetchall()
    return ok({
        "items": [dict(row) for row in rows],
        "categories": categories,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    })


def _missing() -> AppError:
    return AppError("目标图片暂不可用，原文件或对应帧可能已被清理", code="GALLERY_IMAGE_UNAVAILABLE", status_code=404)


@lru_cache(maxsize=128)
def _crop_jpeg(path_text: str, size: int, modified_ns: int, source: str,
               frame_index: int, max_side: int, coords: tuple[float, float, float, float]) -> bytes:
    """缓存缩略图，文件大小或修改时间变化时自然失效。"""
    with _thumbnail_slots:
        return _make_crop(path_text, source, frame_index, max_side, coords)


def _make_crop(path_text: str, source: str, frame_index: int,
               max_side: int, coords: tuple[float, float, float, float]) -> bytes:
    path = Path(path_text)
    if source == "image":
        image = imdecode(path.read_bytes())
    else:
        cap = cv2.VideoCapture(str(path))
        try:
            if not cap.isOpened() or frame_index < 0:
                raise _missing()
            if frame_index and not cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
                raise _missing()
            if abs(cap.get(cv2.CAP_PROP_POS_FRAMES) - frame_index) > 0.5:
                raise _missing()
            success, image = cap.read()
            if not success:
                raise _missing()
            if image.shape[0] * image.shape[1] > MAX_DECODE_PIXELS:
                raise _missing()
            # 视频的检测坐标属于推理时缩放后的帧；按当时的参数重建该坐标系。
            image = resize_long_side(image, max_side)
        finally:
            cap.release()
    if image is None:
        raise _missing()
    h, w = image.shape[:2]
    if not all(math.isfinite(v) for v in coords):
        raise _missing()
    x1, y1, x2, y2 = coords
    left, top = max(0, math.floor(x1)), max(0, math.floor(y1))
    right, bottom = min(w, math.ceil(x2)), min(h, math.ceil(y2))
    if left >= right or top >= bottom:
        raise _missing()
    crop = resize_long_side(image[top:bottom, left:right], 320)
    success, output = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not success:
        raise _missing()
    return output.tobytes()


@router.get("/items/{item_id}/thumbnail", summary="查看目标缩略图")
def thumbnail(item_id: int):
    row = get_connection().execute(
        "SELECT i.frame_index, i.x1, i.y1, i.x2, i.y2, d.task_type, d.saved_name, "
        "d.inference_snapshot FROM detection_items i JOIN detections d ON d.id=i.detection_id "
        "WHERE i.id=? AND d.status='success'", (item_id,)
    ).fetchone()
    if row is None or row["task_type"] not in ("image", "video"):
        raise _missing()
    path = resolve_record_file("saved_name", row["saved_name"] or "")
    if path is None or not path.is_file():
        raise _missing()
    try:
        coords = tuple(float(row[key]) for key in ("x1", "y1", "x2", "y2"))
        max_side = 0
        if row["task_type"] == "video":
            snapshot = json.loads(row["inference_snapshot"] or "{}")
            # 旧记录若没有保存视频缩放参数，无法可靠还原框位置。
            value = snapshot.get("parameters", {}).get("video_max_side")
            if value is None or isinstance(value, bool):
                raise _missing()
            max_side = int(value)
            if max_side < 0 or max_side > 100000:
                raise _missing()
        stat = path.stat()
        data = _crop_jpeg(str(path), stat.st_size, stat.st_mtime_ns, row["task_type"],
                          int(row["frame_index"] or 0), max_side, coords)
    except (OSError, ValueError, TypeError, AttributeError, cv2.error):
        raise _missing()
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
