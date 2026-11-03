"""多记录对比: 把 2~4 条记录的计数口径、类别构成、置信度分布并列成可比较结构。

设计要点:
    - **纯函数**: 输入是 `dao.get_record` / `get_record_items` 的结果, 不查库,
      便于测试与复用。
    - **先对齐口径再比数量**: 不同记录的计数可能来自不同口径(轨迹数 / MaxN /
      检出框), 直接比大小会得出错误结论。因此结果里既给出各记录的原值,
      也给出"口径是否一致"的判定, 以及仅在口径一致时才可用的差值。
    - **类别并集做骨架**: 各类别在不同记录里出现与否不同, 用并集保证同一行
      在所有记录中可横向对齐, 缺失补 0 而不是跳过。
    - **逐条标注适用边界**: 视频与图片、采样明细与全量明细混在一起比时,
      必须把差异讲清楚, 否则图表看起来合理、结论却是错的。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

# 口径不一致时的统一提示, 避免各处措辞漂移
_CALIBER_WARNING = (
    "所选记录的计数口径不完全一致, 上表数值不能直接相减或排序比较。"
    "请先看每条记录的口径标识, 或改选同一口径的记录再比。"
)


def _conf_stats(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """单条记录的置信度统计: 均值 / 中位数 / 四分位 / 阈值敏感性。"""
    values: List[float] = []
    for it in items:
        try:
            conf = float(it.get("confidence"))
        except (TypeError, ValueError):
            continue
        values.append(conf)
    values.sort()
    count = len(values)
    if not count:
        return {"count": 0, "mean": None, "median": None, "p25": None,
                "p75": None, "min": None, "max": None, "sensitivity": []}

    def pick(ratio: float) -> float:
        idx = min(max(int(round(ratio * (count - 1))), 0), count - 1)
        return round(values[idx], 4)

    thresholds = (0.25, 0.5, 0.75, 0.9)
    return {
        "count": count,
        "mean": round(sum(values) / count, 4),
        "median": pick(0.5),
        "p25": pick(0.25),
        "p75": pick(0.75),
        "min": round(values[0], 4),
        "max": round(values[-1], 4),
        "sensitivity": [
            {"threshold": t,
             "kept": sum(1 for v in values if v >= t),
             "ratio": round(sum(1 for v in values if v >= t) / count * 100, 1)}
            for t in thresholds
        ],
    }


def _histogram(items: Sequence[Dict[str, Any]], bins: int = 10) -> List[Dict[str, Any]]:
    """置信度直方图, 分箱边界与 `dao.stats_confidence` 保持一致, 便于对照。"""
    bins = max(5, min(int(bins), 20))
    edges = [round(0.01 + i * (0.99 - 0.01) / bins, 4) for i in range(bins + 1)]
    buckets = [{"lower": edges[i], "upper": edges[i + 1], "count": 0} for i in range(bins)]
    span = edges[-1] - edges[0]
    for it in items:
        try:
            conf = float(it.get("confidence"))
        except (TypeError, ValueError):
            continue
        idx = int((conf - edges[0]) / (span / bins))
        idx = min(max(idx, 0), bins - 1)
        buckets[idx]["count"] += 1
    return buckets


def _caliber(record: Dict[str, Any]) -> Dict[str, Any]:
    """取出该记录的计数口径, 供一致性判定使用。"""
    source = record.get("effective_count_source") or (
        "image_objects" if record.get("task_type") != "video" else "total_detections"
    )
    return {
        "source": source,
        "value": int(record.get("effective_count")
                     or record.get("total_objects") or 0),
    }


def build_comparison(records: Sequence[Dict[str, Any]],
                     items_by_record: Dict[int, Sequence[Dict[str, Any]]],
                     bins: int = 10) -> Dict[str, Any]:
    """组装对比结果。

    参数:
        records:         按用户选择顺序排列的记录字典列表(2~4 条)
        items_by_record: record_id -> 明细列表
        bins:            直方图分箱数
    """
    columns: List[Dict[str, Any]] = []
    class_totals: Dict[str, Dict[int, int]] = {}
    class_names: List[str] = []
    seen_class = set()

    for rec in records:
        rid = int(rec.get("id"))
        items = list(items_by_record.get(rid) or [])
        stats = rec.get("class_stats") or []
        # 类别构成以 class_stats 为准(它来自入库汇总, 不受明细采样上限影响)
        per_class = {str(s.get("class_name")): int(s.get("count") or 0) for s in stats}
        for name in per_class:
            if name not in seen_class:
                seen_class.add(name)
                class_names.append(name)
        class_totals[str(rid)] = per_class

        caliber = _caliber(rec)
        maxn = rec.get("maxn") or {}
        columns.append({
            "record_id": rid,
            "source_name": rec.get("source_name"),
            "task_type": rec.get("task_type"),
            "created_at": rec.get("created_at"),
            "result_url": (f"/data/results/{rec['result_image']}"
                           if rec.get("result_image") else None),
            "result_video_url": (f"/data/results/{rec['result_video']}"
                                 if rec.get("result_video") else None),
            "thumb_url": (f"/data/results/{rec['result_image']}"
                          if rec.get("result_image") else None),
            "site_label": rec.get("site_label"),
            "transect_label": rec.get("transect_label"),
            "survey_round": rec.get("survey_round"),
            "depth_m": rec.get("depth_m"),
            "conf_threshold": rec.get("conf_threshold"),
            "caliber": caliber,
            "total_detections": int(rec.get("total_detections") or 0),
            "tracked_objects": int(rec.get("tracked_objects") or 0),
            "maxn_total": int(maxn.get("maxn_total") or rec.get("maxn_total") or 0),
            "cpunx": maxn.get("cpunx") if maxn.get("cpunx") is not None
                     else rec.get("cpunx"),
            "class_count": len(per_class),
            "conf": _conf_stats(items),
            "histogram": _histogram(items, bins),
            "items_sampled": len(items),
            "params": (rec.get("inference_snapshot") or {}).get("parameters") or {},
        })

    # 类别构成: 并集骨架 + 缺失补 0, 保证行在所有列上可横向对齐
    class_matrix = []
    for name in sorted(class_names,
                       key=lambda n: -sum(class_totals[c][n] for c in class_totals
                                          if n in class_totals[c])):
        row = {"class_name": name, "counts": {}}
        for col in columns:
            rid = str(col["record_id"])
            row["counts"][rid] = class_totals[rid].get(name, 0)
        class_matrix.append(row)

    sources = {col["caliber"]["source"] for col in columns}
    comparable = len(sources) == 1
    warnings: List[str] = []
    if not comparable:
        warnings.append(_CALIBER_WARNING)
    if len({col["task_type"] for col in columns}) > 1:
        warnings.append(
            "所选记录中同时包含图片与视频。图片的计数即检出框数, 视频还涉及抽帧与跟踪, "
            "二者的数量级不可直接比较。"
        )
    if any(col["task_type"] == "video" and col["conf"]["count"] < col["total_detections"]
           for col in columns):
        warnings.append(
            "视频记录的结果明细为采样入库, 置信度分布的样本量小于该记录的实际检出框数, "
            "分布形状可比、绝对计数不可比。"
        )

    return {
        "columns": columns,
        "class_matrix": class_matrix,
        "class_names": class_names,
        "conf_histograms": [
            {"record_id": col["record_id"], "bins": col["histogram"]}
            for col in columns
        ],
        "comparable": comparable,
        "caliber_sources": sorted(sources),
        "warnings": warnings,
        "scope": {
            "records": len(columns),
            "selection": "按用户勾选的记录逐条取数, 不做去重也不做时间过滤",
            "limitation": (
                "类别构成取自入库的类别汇总(对视频为采样汇总); 置信度分布取自明细样本。"
                "两条记录的差异可能同时来自模型版本、置信度阈值、抽帧间隔与跟踪设置, "
                "对比时需结合 parameters 逐项核对, 不能仅凭总量差异归因于模型本身。"
            ),
        },
    }
