"""数据统计接口: 总览 / 趋势 / 分类分布 / 最近记录。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Literal

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from app.db import dao
from app.utils.response import ok

router = APIRouter(prefix="/api/stats", tags=["数据统计"])


_ECOLOGY_SCOPE = {
    "task_type": "image", "deduplicate": True,
    "selection": "每个源图片仅取最新成功检测；按检测日期筛选，不包含视频",
    "limitation": "仅描述已检出类别的分布，不代表实际物种数、个体总量或生态质量。",
}


def _as_nonneg_int(value: Any) -> int:
    """把"可能是 FastAPI 参数元数据对象"的值安全地退化成非负整数。

    直接以 Python 方式调用路由函数(单元测试、脚本)时, 带 `Query(...)` 默认值的
    参数拿到的是 Query 对象本身而不是它的 default —— 直接 int() 会抛
    TypeError: int() argument must be ..., not 'Query'。
    经 HTTP 访问时 FastAPI 已完成解析, 拿到的是普通 int, 这里原样返回。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    # 走到这里的可能是 Query/Path 等参数元数据对象, 取它的 default 作为真实值
    default = getattr(value, "default", None)
    if default is not None and isinstance(default, (int, float)):
        return max(0, int(default))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@router.get("/ecology", summary="去重图片的检出类别分布指标")
async def ecology(
    days: int = Query(0, ge=0, le=400, description="仅统计最近 N 天(自然日), 0 表示全部历史"),
    hours: int = Query(0, ge=0, le=168,
                       description="滚动窗口小时数, 大于 0 时优先于 days(如 24 = 最近 24 小时)"),
) -> Dict[str, Any]:
    """基于类别计数计算生态多样性指标(Shannon / Simpson / Pielou / Margalef)。

    纯统计计算, 不涉及模型推理。`abundance` 中的 `cumulative_ratio` 供帕累托图使用;
    `interpretation` 是中文定性解读, 用于辅助阅读而非替代专业判读。

    `hours` 与 `days` 并存而不是让 `days=24` 兼表 24 小时: 两者窗口口径不同
    (滚动 24 小时 vs 自然日), 共用参数会让 scope 说明与既有接口语义一起含混。
    """
    from app.core.ecology import build_payload

    span, window = _as_nonneg_int(days), _as_nonneg_int(hours)
    classes = await run_in_threadpool(
        lambda: dao.stats_classes(days=span, deduplicate=True, task_type="image",
                                  hours=window)
    )
    payload = build_payload(classes)
    payload["scope"] = {**_ECOLOGY_SCOPE, "days": span}
    if window > 0:
        payload["scope"].update({
            "hours": window,
            "window": "滚动窗口: 当前时刻往前 %d 小时(按时间戳比较, 非自然日)" % window,
        })
    return ok(payload)


@router.get("/ecology-trend", summary="生态指标随时间变化")
async def ecology_trend(
    days: int = Query(14, ge=2, le=60, description="按天口径的时间跨度"),
    hours: int = Query(0, ge=0, le=168,
                       description="按小时口径的时间跨度, 大于 0 时优先于 days(如 24 = 最近 24 小时)"),
) -> Dict[str, Any]:
    """按天(或按小时)计算 H' 与均匀度, 用于观察群落多样性的时间演变。

    对每个时间桶的类别分布单独计算指数 —— 直接复用 class_trend 的计数,
    不额外查询数据库。

    `hours>0` 时走小时桶: 24 小时窗口如果仍按天分桶, 最多只有 2 个点,
    曲线退化成单点, 看不出"最近一天之内"的任何变化。
    """
    from app.core.ecology import compute_indicators

    span, window = _as_nonneg_int(days), _as_nonneg_int(hours)
    trend = await run_in_threadpool(
        dao.stats_class_trend, span, "day", deduplicate=True, task_type="image",
        hours=window,
    )
    labels = trend.get("labels") or []
    series = trend.get("series") or []
    granularity = trend.get("granularity") or "day"

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
        scope = {**_ECOLOGY_SCOPE, "days": span, "granularity": granularity}
        if window > 0:
            scope.update({
                "hours": window,
                "window": "滚动窗口: 当前时刻往前 %d 小时, 按整点分桶" % window,
            })
        return {"points": points, "scope": scope}

    return ok(await run_in_threadpool(_calc))


@router.get("/overview", summary="总览统计")
async def overview() -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_overview))


@router.get("/trend", summary="最近 N 个时间桶的检测趋势")
async def trend(
    days: int = Query(7, ge=1, le=400),
    granularity: Literal["day", "week"] = Query("day", description="day 按天 | week 按周"),
) -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_trend, days, granularity))


@router.get("/class-trend", summary="各类别随时间的检出数量")
async def class_trend(
    days: int = Query(7, ge=1, le=400),
    granularity: Literal["day", "week"] = Query("day"),
) -> Dict[str, Any]:
    return ok(await run_in_threadpool(dao.stats_class_trend, days, granularity))


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
    days: int = Query(7, ge=0, le=400, description="0 表示全部历史"),
    granularity: Literal["day", "week"] = Query("day", description="day 按天 | week 按周"),
) -> Dict[str, Any]:
    """一次请求返回统计页所需的全部数据, 减少前端请求次数。

    `days=0` 是新增的"今日以外"的第四种时间选项: 全量口径。此时趋势图仍按
    7 天渲染(趋势必须有时间轴), 而分类/站点/置信度切到全量。
    """
    trend_days = days if days > 0 else 7
    filter_days = days
    if days > 0 and granularity == "week":
        # Week buckets start on Monday, including the current partial week.
        filter_days = (days - 1) * 7 + dt.date.today().weekday() + 1
    return ok({
        "overview": await run_in_threadpool(dao.stats_overview),
        "trend": await run_in_threadpool(dao.stats_trend, trend_days, granularity),
        "class_trend": await run_in_threadpool(dao.stats_class_trend, trend_days, granularity),
        "hourly": await run_in_threadpool(dao.stats_hourly),
        "classes": await run_in_threadpool(dao.stats_classes, filter_days),
        "types": await run_in_threadpool(dao.stats_type_distribution),
        "recent": await run_in_threadpool(dao.stats_recent, 8),
        # 新增三项: 今日构成 / 站点维度 / 置信度分布, 与统计页看板一一对应
        "today": await run_in_threadpool(dao.stats_today),
        "sites": await run_in_threadpool(dao.stats_sites, filter_days, "", True),
        "confidence": await run_in_threadpool(dao.stats_confidence, filter_days, 10, ""),
    })


@router.get("/today", summary="今日(最近 N 小时)检测数据构成")
async def today(
    hours: int = Query(24, ge=1, le=168, description="统计窗口(小时), 默认 24 即今日滚动窗口"),
) -> Dict[str, Any]:
    """今日检测的构成明细: 分类型 / 分站点 / 分时段 / 分计数口径。

    作为统计页"今日检测数据"选项的数据源。窗口按小时滚动, 因此
    "今日"是"最近 24 小时", 而不是自然日 00:00 切分 —— 后者在跨零点时会
    把连续作业切成两段, 与调查实际不符; 需要自然日口径时用 `days=1` 的其它接口。
    """
    return ok(await run_in_threadpool(dao.stats_today, hours))


@router.get("/sites", summary="站点 / 样方维度分析")
async def sites(
    days: int = Query(0, ge=0, le=400, description="仅统计最近 N 天, 0 表示全部历史"),
    task_type: Literal["", "image", "video"] = Query("", description="留空表示不限类型"),
    deduplicate: bool = Query(True, description="同一源素材仅取最新成功检测"),
) -> Dict[str, Any]:
    """按站点汇总, 样方作为第二层下钻进入。

    返回结构即下钻结构: 站点 -> 样方 -> (记录列表由前端再调 /api/records)。
    未填写站点的记录归入"未标注站点"并保留计数, 不会被静默丢弃。
    """
    rows = await run_in_threadpool(dao.stats_sites, days, task_type, deduplicate)
    return ok({
        "items": rows,
        "total_sites": len(rows),
        "total_objects": sum(item["objects"] for item in rows),
        "scope": {
            "days": days, "task_type": task_type or "all", "deduplicate": deduplicate,
            "selection": "按稳定站点编号及样方分组；已删除或旧记录按历史站名分组；无站名记录归入“未标注站点”",
            "limitation": "值为各记录 total_objects 之和, 未做面积或努力量归一; "
                          "跨站点比较前请结合调查时长与样方面积。",
        },
    })


@router.get("/confidence", summary="置信度分布看板")
async def confidence(
    days: int = Query(0, ge=0, le=400, description="仅统计最近 N 天, 0 表示全部历史"),
    bins: int = Query(10, ge=5, le=20, description="直方图分桶数"),
    task_type: Literal["", "image", "video"] = Query("", description="留空表示不限类型"),
) -> Dict[str, Any]:
    """检出框置信度分布 + 阈值敏感性, 用于判断当前阈值是否过松或过严。"""
    return ok(await run_in_threadpool(dao.stats_confidence, days, bins, task_type))
