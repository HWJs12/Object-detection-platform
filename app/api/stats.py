"""数据统计接口: 总览 / 趋势 / 分类分布 / 最近记录。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from app.db import dao
from app.utils.response import ok

router = APIRouter(prefix="/api/stats", tags=["数据统计"])


@router.get("/ecology", summary="生态多样性指标")
async def ecology(
    days: int = Query(0, ge=0, le=400, description="仅统计最近 N 天, 0 表示全部历史"),
) -> Dict[str, Any]:
    """基于类别计数计算生态多样性指标(Shannon / Simpson / Pielou / Margalef)。

    纯统计计算, 不涉及模型推理。`abundance` 中的 `cumulative_ratio` 供帕累托图使用;
    `interpretation` 是中文定性解读, 用于辅助阅读而非替代专业判读。
    """
    from app.core.ecology import build_payload

    classes = await run_in_threadpool(
        lambda: dao.stats_classes(days=days) if days > 0 else dao.stats_classes()
    )
    return ok(build_payload(classes))


@router.get("/ecology-trend", summary="生态指标随时间变化")
async def ecology_trend(days: int = Query(14, ge=2, le=60)) -> Dict[str, Any]:
    """按天计算 H' 与均匀度, 用于观察群落多样性的时间演变。

    对每一天的类别分布单独计算指数 —— 直接复用 class_trend 的按天计数,
    不额外查询数据库。
    """
    from app.core.ecology import compute_indicators

    trend = await run_in_threadpool(dao.stats_class_trend, days)
    labels = trend.get("labels") or []
    series = trend.get("series") or []

    def _calc() -> Dict[str, Any]:
        points = []
        for i, label in enumerate(labels):
            counts = [
                {"class_name": s["name"], "count": (s["data"][i] if i < len(s["data"]) else 0)}
                for s in series
            ]
            r = compute_indicators(counts)
            points.append({
                "label": label,
                "shannon": r["shannon"],
                "pielou": r["pielou"],
                "richness": r["richness"],
                "total": r["total"],
            })
        return {"points": points}

    return ok(await run_in_threadpool(_calc))


@router.get("/overview", summary="总览统计")
async def overview() -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_overview))


@router.get("/trend", summary="最近 N 个时间桶的检测趋势")
async def trend(
    days: int = Query(7, ge=1, le=400),
    granularity: str = Query("day", description="day 按天 | week 按周"),
) -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_trend, days, granularity))


@router.get("/class-trend", summary="各类别随时间的检出数量")
async def class_trend(days: int = Query(7, ge=1, le=60)) -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_class_trend, days))


@router.get("/hourly", summary="24 小时检测时段分布")
async def hourly() -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_hourly))


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
async def dashboard(
    days: int = Query(7, ge=1, le=400),
    granularity: str = Query("day", description="day 按天 | week 按周"),
) -> Dict[str, Any]:
    """一次请求返回统计页所需的全部数据, 减少前端请求次数。"""
    return ok({
        "overview": await run_in_threadpool(dao.stats_overview),
        "trend": await run_in_threadpool(dao.stats_trend, days, granularity),
        "class_trend": await run_in_threadpool(dao.stats_class_trend, min(days, 60)),
        "hourly": await run_in_threadpool(dao.stats_hourly),
        "classes": await run_in_threadpool(dao.stats_classes),
        "types": await run_in_threadpool(dao.stats_type_distribution),
        "recent": await run_in_threadpool(dao.stats_recent, 8),
    })
