"""数据统计接口: 总览 / 趋势 / 分类分布 / 最近记录。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from app.db import dao
from app.utils.response import ok

router = APIRouter(prefix="/api/stats", tags=["数据统计"])


@router.get("/overview", summary="总览统计")
async def overview() -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_overview))


@router.get("/trend", summary="最近 N 天检测趋势")
async def trend(days: int = Query(7, ge=1, le=60)) -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_trend, days))


@router.get("/classes", summary="各类别目标数量")
async def classes() -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_classes))


@router.get("/types", summary="图片/视频检测次数分布")
async def types() -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_type_distribution))


@router.get("/recent", summary="最近的检测记录")
async def recent(limit: int = Query(8, ge=1, le=50)) -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_recent, limit))


@router.get("/dashboard", summary="统计页聚合数据")
async def dashboard(days: int = Query(7, ge=1, le=60)) -> Dict[str, Any]:
    """一次请求返回统计页所需的全部数据, 减少前端请求次数。"""
    return ok({
        "overview": await run_in_threadpool(dao.stats_overview),
        "trend": await run_in_threadpool(dao.stats_trend, days),
        "classes": await run_in_threadpool(dao.stats_classes),
        "types": await run_in_threadpool(dao.stats_type_distribution),
        "recent": await run_in_threadpool(dao.stats_recent, 8),
    })
