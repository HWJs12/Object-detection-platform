"""历史记录接口: 查询 / 详情 / 删除。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from app.db import dao
from app.utils.exceptions import RecordNotFoundError
from app.utils.response import ok

router = APIRouter(prefix="/api/records", tags=["历史记录"])


@router.get("", summary="分页查询检测记录")
async def list_records(
    task_type: str = Query("", description="image | video, 留空为全部"),
    keyword: str = Query("", description="文件名关键字"),
    date_from: str = Query("", description="起始日期 YYYY-MM-DD"),
    date_to: str = Query("", description="结束日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
) -> Dict[str, Any]:
    rows, total = dao.list_records(
        task_type=task_type, keyword=keyword, date_from=date_from,
        date_to=date_to, page=page, page_size=page_size,
    )
    # 图片记录补充结果图访问地址
    for row in rows:
        if row.get("result_image"):
            row["result_url"] = f"/data/results/{row['result_image']}"
    return ok({
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    })


@router.get("/{record_id}", summary="查询单条记录详情")
async def get_record(record_id: int) -> Dict[str, Any]:
    record = dao.get_record(record_id)
    if not record:
        raise RecordNotFoundError(f"记录不存在: {record_id}")

    if record.get("result_image"):
        record["result_url"] = f"/data/results/{record['result_image']}"

    record["items"] = dao.get_record_items(record_id)
    record["class_stats"] = dao.get_record_class_stats(record_id)
    record["frame_stats"] = dao.get_record_frame_stats(record_id)
    return ok(record)


@router.delete("/{record_id}", summary="删除检测记录")
async def delete_record(record_id: int) -> Dict[str, Any]:
    if not dao.delete_record(record_id):
        raise RecordNotFoundError(f"记录不存在: {record_id}")
    return ok({"id": record_id}, "记录已删除")
