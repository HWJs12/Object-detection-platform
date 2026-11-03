"""多记录对比接口: 并列展示 2~4 条记录的计数口径、类别构成与置信度分布。

为什么单独一个路由而不是复用 `/api/records/{id}`:
    对比要一次取齐全部记录并做跨记录对齐(类别并集、口径一致性判定),
    前端串行请求 4 次既慢又难保证前后一致, 还容易漏掉口径不一致的告警。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool

from app.core import compare
from app.db import dao
from app.utils.exceptions import AppError
from app.utils.response import ok

router = APIRouter(prefix="/api/compare", tags=["多记录对比"])

# 少于 2 条没有比较意义, 多于 4 条在前端无法并排阅读
_MIN_RECORDS = 2
_MAX_RECORDS = 4


def _clean_ids(raw: Any) -> List[int]:
    if not isinstance(raw, list):
        raise AppError("请提供要对比的记录 ID 列表", code="EMPTY_RECORDS")
    ids: List[int] = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise AppError(f"非法的记录 ID: {value}", code="INVALID_RECORD_ID")
    # 去重但保留用户勾选顺序
    deduped: List[int] = []
    for rid in ids:
        if rid not in deduped:
            deduped.append(rid)
    return deduped


@router.post("", summary="对比 2~4 条检测记录")
async def compare_records(
    payload: Dict[str, Any] = Body(
        ..., description='{"record_ids": [1,2], "bins": 10}'
    ),
) -> Dict[str, Any]:
    """并列对比所选记录的类别构成、置信度分布与计数口径。

    请求体:
        {"record_ids": [101, 102], "bins": 10}
        - record_ids: 2~4 条记录, 顺序即对比顺序
        - bins:       置信度直方图分箱数, 默认 10, 范围 5~20
    """
    body = payload or {}
    ids = _clean_ids(body.get("record_ids"))
    try:
        bins = int(body.get("bins") or 10)
    except (TypeError, ValueError):
        bins = 10
    bins = max(5, min(bins, 20))

    if not ids:
        raise AppError("请提供要对比的记录 ID 列表", code="EMPTY_RECORDS")
    if len(ids) < _MIN_RECORDS:
        raise AppError(
            f"至少选择 {_MIN_RECORDS} 条记录才能对比", code="TOO_FEW_RECORDS"
        )
    if len(ids) > _MAX_RECORDS:
        raise AppError(
            f"单次最多对比 {_MAX_RECORDS} 条记录, 当前 {len(ids)} 条",
            code="TOO_MANY_RECORDS",
        )

    records = dao.get_records_ordered(ids)
    if len(records) < _MIN_RECORDS:
        raise AppError(
            f"所选记录仅存 {len(records)} 条, 不足以对比(可能已被删除)",
            code="RECORDS_NOT_FOUND",
        )

    def _load_items() -> Dict[int, List[Dict[str, Any]]]:
        # 明细只用于置信度统计, 取 500 条采样即可; 类别构成另有汇总表可用
        return {int(rec["id"]): dao.get_record_items(int(rec["id"]), limit=500)
                for rec in records}

    # 类别构成必须来自汇总表(detection_class_stats), 而不是从采样明细现数:
    # 视频明细有入库上限, 用明细反推会让视频的类别构成系统性偏小。
    for rec in records:
        rec["class_stats"] = dao.get_record_class_stats(int(rec["id"]))

    items_by_record = await run_in_threadpool(_load_items)
    result = await run_in_threadpool(compare.build_comparison, records,
                                     items_by_record, bins)
    result["requested_ids"] = ids
    result["missing_ids"] = [rid for rid in ids if rid not in {r["id"] for r in records}]
    return ok(result)
