"""数据访问层(DAO): 所有 SQL 均在此集中, 一律使用参数化查询防注入。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.db.database import get_connection, transaction
from app.utils.survey import SITE_UNLABELED, TRANSECT_UNLABELED

# ---------------------------------------------------------------- 写入


_JSON_COLUMNS = ("inference_snapshot", "maxn_per_class")


def insert_detection(data: Dict[str, Any]) -> int:
    """写入一条检测主记录, 返回自增 id。"""
    keys = [
        "task_type", "source_name", "saved_name", "result_image", "result_video", "created_at",
        "total_objects", "image_width", "image_height", "inference_ms",
        "frames_processed", "avg_fps", "duration_sec", "conf_threshold",
        "unique_objects", "total_detections", "tracked_objects",
        "counting_mode", "unique_count_valid", "source_hash", "inference_snapshot",
        "site_id", "site", "transect", "depth_m", "survey_round",
        "maxn_per_class", "maxn_total", "cpunx",
        "status", "message",
    ]
    cols = [k for k in keys if k in data]
    sql = f"INSERT INTO detections ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})"
    with transaction() as cur:
        data = dict(data)
        if data.get('site_id') is not None:
            station = cur.execute('SELECT name FROM survey_stations WHERE id=?', (data['site_id'],)).fetchone()
            if station is None:
                # 上传时已验证并保存站点名称。处理中删除站点不应丢弃检测结果。
                if not isinstance(data.get('site'), str) or not data['site'].strip():
                    from app.utils.exceptions import AppError
                    raise AppError('关联站点不存在', code='STATION_NOT_FOUND', status_code=404)
                data['site_id'] = None
            else:
                data['site'] = station['name']
            if 'site' not in cols:
                cols.append('site')
                sql = f"INSERT INTO detections ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})"
        values = [json.dumps(data[c], ensure_ascii=False)
                  if c in _JSON_COLUMNS and isinstance(data[c], (dict, list)) else data[c]
                  for c in cols]
        cur.execute(sql, values)
        return int(cur.lastrowid)


def insert_items(detection_id: int, items: Sequence[Dict[str, Any]]) -> None:
    """批量写入检测明细(每个目标一条)。"""
    if not items:
        return
    sql = (
        "INSERT INTO detection_items "
        "(detection_id, frame_index, class_id, class_name, confidence, x1, y1, x2, y2) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    rows = []
    for it in items:
        bbox = it.get("bbox") or [None, None, None, None]
        rows.append((
            detection_id,
            int(it.get("frame_index", 0)),
            int(it.get("class_id", -1)),
            str(it.get("class_name", "unknown")),
            float(it.get("confidence", 0.0)),
            *[float(v) if v is not None else None for v in bbox],
        ))
    with transaction() as cur:
        cur.executemany(sql, rows)


def insert_class_stats(detection_id: int, stats: Sequence[Tuple[int, str, int]]) -> None:
    """写入类别统计: stats = [(class_id, class_name, count), ...]。"""
    if not stats:
        return
    sql = (
        "INSERT INTO detection_class_stats (detection_id, class_id, class_name, object_count) "
        "VALUES (?, ?, ?, ?)"
    )
    with transaction() as cur:
        cur.executemany(sql, [(detection_id, cid, name, cnt) for cid, name, cnt in stats])


def insert_frame_stats(detection_id: int,
                       rows: Sequence[Tuple[int, float, int, Optional[Dict[str, int]]]]) -> None:
    """写入视频逐帧统计。

    rows = [(frame_index, timestamp_sec, total_objects, class_counts), ...]
    class_counts 为当帧各类别计数(可空), 供 MaxN 按类别取峰值使用。
    """
    if not rows:
        return
    sql = (
        "INSERT INTO video_frame_stats "
        "(detection_id, frame_index, timestamp_sec, total_objects, class_counts) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    with transaction() as cur:
        payload = []
        for row in rows:
            frame_index, ts, total = row[0], row[1], row[2]
            per_class = row[3] if len(row) > 3 else None
            payload.append((
                detection_id, frame_index, ts, total,
                json.dumps(per_class or {}, ensure_ascii=False),
            ))
        cur.executemany(sql, payload)


def delete_record(detection_id: int) -> bool:
    """删除一条记录(级联删除明细), 返回是否删除成功。"""
    with transaction() as cur:
        cur.execute("DELETE FROM detections WHERE id = ?", (detection_id,))
        return cur.rowcount > 0


def save_detection(data: Dict[str, Any], items: Sequence[Dict[str, Any]],
                   class_stats: Sequence[Tuple[int, str, int]],
                   frame_stats: Sequence[Tuple[Any, ...]] = ()) -> int:
    """主记录、目标明细、分类与帧统计作为一个完整业务事务提交。"""
    with transaction():
        record_id = insert_detection(data)
        insert_items(record_id, items)
        insert_class_stats(record_id, class_stats)
        insert_frame_stats(record_id, frame_stats)
    return record_id


# ---------------------------------------------------------------- 查询


def get_record(detection_id: int) -> Optional[Dict[str, Any]]:
    row = get_connection().execute(
        "SELECT * FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()
    return _record_dict(row) if row else None


def _record_dict(row) -> Dict[str, Any]:
    record = dict(row)
    for column in _JSON_COLUMNS:
        raw = record.get(column)
        if isinstance(raw, (dict, list)):
            continue
        try:
            parsed = json.loads(raw or "{}")
        except (ValueError, TypeError):
            parsed = {}
        record[column] = parsed if isinstance(parsed, dict) else {}
    record["unique_count_valid"] = bool(record.get("unique_count_valid"))
    # 调查维度: 空串统一为 None, 便于前端判断"未标注"而不是显示空字符串
    for column in ("site", "transect", "survey_round"):
        if not record.get(column):
            record[column] = None
    record["site_label"] = record.get("site") or "未标注站点"
    record["transect_label"] = record.get("transect") or "未标注样方"

    if record.get("task_type") == "image":
        record["unique_objects"] = int(record.get("total_objects") or 0)
        record["unique_count_valid"] = True

    # ---- 计数取值兜底 ----
    # 原则: 三类计数(逐帧检出框 / 跨帧去重 / MaxN)始终有一个可展示,
    # 不再把 unique_objects 置为 null 让界面出现"不可用"空位。
    # 具体取用哪一个由 counting_mode 与 unique_count_valid 决定, 前端据此标注口径。
    record["effective_count"] = _effective_count(record)
    record["effective_count_source"] = _effective_count_source(record)
    record["maxn"] = _maxn_summary(record)
    return record


def _maxn_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """整理 MaxN 相对丰度口径, 供界面与报告直接消费。

    MaxN 与"轨迹数"是两套独立口径:
      - 轨迹数依赖跨帧关联, 跟踪不完整时失去依据;
      - MaxN 只要求同一帧内不重复计数, 跟踪失效时依然成立。
    因此 MaxN 是唯一计数为空时的兜底口径, 但它仍是相对丰度, 不是个体总量。
    """
    per_class = record.get("maxn_per_class") or {}
    if not isinstance(per_class, dict):
        per_class = {}
    per_class = {str(k): int(v or 0) for k, v in per_class.items()}
    total = int(record.get("maxn_total") or 0)
    duration = float(record.get("duration_sec") or 0.0)
    cpunx = record.get("cpunx")
    if cpunx is None and duration > 0 and total > 0:
        # 兜底: 历史行缺 cpunx 时由 MaxN 与时长现算, 避免返回空值
        cpunx = round(total / (duration / 3600.0), 1)
    available = total > 0 and record.get("task_type") == "video" and bool(per_class)
    return {
        "available": available,
        "per_class": per_class,
        "maxn_total": total,
        "cpunx": round(float(cpunx), 1) if cpunx is not None else None,
        "unit": "条/小时",
        "scope": "单帧内同类别的最大同时出现数量(相对丰度)",
        "limitation": "单帧峰值通常低估实际经过的目标总数; 不等同于生物个体数或绝对密度。",
    }


def _effective_count(record: Dict[str, Any]) -> int:
    """返回该记录最该被展示的计数值。图片为检出框数, 视频按口径优先级取。"""
    if record.get("task_type") != "video":
        return int(record.get("total_objects") or 0)
    if record["unique_count_valid"] and record.get("unique_objects") is not None:
        return int(record.get("unique_objects") or 0)
    maxn_total = int(record.get("maxn_total") or 0)
    if maxn_total > 0:
        return maxn_total
    return int(record.get("total_detections") or record.get("total_objects") or 0)


def _effective_count_source(record: Dict[str, Any]) -> str:
    """说明 effective_count 用的是哪套口径, 供界面与下钻逐层解释。"""
    if record.get("task_type") != "video":
        return "image_objects"
    if record["unique_count_valid"] and record.get("unique_objects") is not None:
        return "unique_tracks"
    if int(record.get("maxn_total") or 0) > 0:
        return "maxn"
    return "total_detections"


def get_record_items(detection_id: int, limit: Optional[int] = 500) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM detection_items WHERE detection_id = ? ORDER BY frame_index, confidence DESC"
    args = (detection_id,)
    if limit is not None:
        sql += " LIMIT ?"
        args += (max(0, int(limit)),)
    rows = get_connection().execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_record_class_stats(detection_id: int) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT class_name, SUM(object_count) AS count FROM detection_class_stats "
        "WHERE detection_id = ? GROUP BY class_name ORDER BY count DESC",
        (detection_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_record_frame_stats(detection_id: int, limit: int = 1000) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT frame_index, timestamp_sec, total_objects, class_counts FROM video_frame_stats "
        "WHERE detection_id = ? ORDER BY frame_index LIMIT ?",
        (detection_id, limit),
    ).fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            parsed = json.loads(item.get("class_counts") or "{}")
        except (ValueError, TypeError):
            parsed = {}
        item["class_counts"] = parsed if isinstance(parsed, dict) else {}
        result.append(item)
    return result


def get_record_sources(detection_id: int) -> List[Dict[str, Any]]:
    """返回"构成某个统计数字"的原始记录列表, 供界面下钻使用。

    图片记录的计数值等于其明细框数, 下钻即明细本身;
    视频记录的数值来自逐帧采样, 下钻指向逐帧记录。
    """
    record = get_record(detection_id)
    if not record:
        return []
    if record.get("task_type") == "video":
        return get_record_frame_stats(detection_id)
    return get_record_items(detection_id)


def get_records_ordered(ids: Sequence[int]) -> List[Dict[str, Any]]:
    """按传入的 ID 顺序批量取记录, 跳过不存在的。

    多记录对比要求"勾选顺序即展示顺序", 而 `IN (...)` 的返回顺序由数据库决定,
    因此这里先建映射再按 ids 还原顺序。重复 ID 只保留首次出现的位置。
    """
    cleaned: List[int] = []
    for raw in ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        return []

    placeholders = ",".join("?" * len(cleaned))
    rows = get_connection().execute(
        f"SELECT * FROM detections WHERE id IN ({placeholders})", tuple(cleaned)
    ).fetchall()
    by_id = {int(row["id"]): _record_dict(row) for row in rows}
    return [by_id[rid] for rid in cleaned if rid in by_id]


def list_records(task_type: str = "", keyword: str = "", date_from: str = "",
                 date_to: str = "", page: int = 1, page_size: int = 10, site_id: Optional[int] = None
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """分页查询检测记录, 支持类型 / 文件名关键字 / 日期区间过滤。"""
    where, args = ["1=1"], []

    if site_id is not None:
        where.append("site_id = ?")
        args.append(site_id)
    if task_type in ("image", "video"):
        where.append("task_type = ?")
        args.append(task_type)
    if keyword:
        where.append("(source_name LIKE ? OR saved_name LIKE ?)")
        args.extend([f"%{keyword}%", f"%{keyword}%"])
    if date_from:
        where.append("date(created_at) >= date(?)")
        args.append(date_from)
    if date_to:
        where.append("date(created_at) <= date(?)")
        args.append(date_to)

    clause = " AND ".join(where)
    conn = get_connection()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM detections WHERE {clause}", args
    ).fetchone()["c"]

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    offset = (page - 1) * page_size

    rows = conn.execute(
        f"SELECT * FROM detections WHERE {clause} "
        f"ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, page_size, offset),
    ).fetchall()
    return [_record_dict(r) for r in rows], int(total)


# ---------------------------------------------------------------- 统计


def stats_overview() -> Dict[str, Any]:
    """总览统计: 今日 / 累计检测次数, 目标总数, 分类数等。"""
    conn = get_connection()
    today = dt.date.today().isoformat()

    today_count = conn.execute(
        "SELECT COUNT(*) AS c FROM detections WHERE date(created_at) = date(?)", (today,)
    ).fetchone()["c"]
    total_count = conn.execute("SELECT COUNT(*) AS c FROM detections").fetchone()["c"]
    total_objects = conn.execute(
        "SELECT COALESCE(SUM(total_objects), 0) AS s FROM detections"
    ).fetchone()["s"]
    image_count = conn.execute(
        "SELECT COUNT(*) AS c FROM detections WHERE task_type = 'image'"
    ).fetchone()["c"]
    video_count = conn.execute(
        "SELECT COUNT(*) AS c FROM detections WHERE task_type = 'video'"
    ).fetchone()["c"]
    class_count = conn.execute(
        "SELECT COUNT(DISTINCT class_name) AS c FROM detection_class_stats"
    ).fetchone()["c"]
    avg_ms = conn.execute(
        "SELECT COALESCE(AVG(inference_ms), 0) AS a FROM detections WHERE inference_ms IS NOT NULL"
    ).fetchone()["a"]
    today_objects = conn.execute(
        "SELECT COALESCE(SUM(total_objects), 0) AS s FROM detections WHERE date(created_at) = date(?)",
        (today,),
    ).fetchone()["s"]
    latest = conn.execute("SELECT MAX(created_at) AS m FROM detections").fetchone()["m"]

    return {
        "today_count": today_count,
        "today_objects": today_objects,
        "total_count": total_count,
        "total_objects": total_objects,
        "image_count": image_count,
        "video_count": video_count,
        "class_count": class_count,
        "avg_inference_ms": round(float(avg_ms or 0), 1),
        "latest_at": latest,
    }


def stats_trend(days: int = 7, granularity: str = "day") -> List[Dict[str, Any]]:
    """最近 N 个时间桶的检测次数与目标数(缺失桶补 0)。

    granularity: "day" 按天 | "week" 按周(周一为起点)
    """
    days = max(1, min(int(days), 400))
    granularity = "week" if str(granularity).lower() == "week" else "day"
    conn = get_connection()
    today = dt.date.today()

    if granularity == "week":
        this_monday = today - dt.timedelta(days=today.weekday())
        start = this_monday - dt.timedelta(weeks=days - 1)
        rows = conn.execute(
            "SELECT date(created_at, 'weekday 0', '-6 days') AS bucket, "
            "COUNT(*) AS count, COALESCE(SUM(total_objects), 0) AS objects "
            "FROM detections WHERE date(created_at) >= date(?) "
            "GROUP BY bucket",
            (start.isoformat(),),
        ).fetchall()
        mapping = {r["bucket"]: dict(r) for r in rows}
        result = []
        for i in range(days):
            day = start + dt.timedelta(weeks=i)
            item = mapping.get(day.isoformat(), {"day": day.isoformat(), "count": 0, "objects": 0})
            result.append({
                "day": day.isoformat(),
                "label": f"{day.strftime('%m-%d')} 周",
                "count": int(item["count"]),
                "objects": int(item["objects"]),
            })
        return result

    start = (today - dt.timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT date(created_at) AS day, COUNT(*) AS count, "
        "COALESCE(SUM(total_objects), 0) AS objects "
        "FROM detections WHERE date(created_at) >= date(?) "
        "GROUP BY date(created_at)",
        (start,),
    ).fetchall()
    mapping = {r["day"]: dict(r) for r in rows}

    result = []
    for i in range(days):
        day = (today - dt.timedelta(days=days - 1 - i)).isoformat()
        item = mapping.get(day, {"day": day, "count": 0, "objects": 0})
        result.append({
            "day": day,
            "label": day[5:],
            "count": int(item["count"]),
            "objects": int(item["objects"]),
        })
    return result


def _analysis_selection(deduplicate: bool = False, task_type: str = "") -> Tuple[str, tuple]:
    """先选每份素材最新成功分析, 时间过滤必须在去重之后执行。"""
    args: tuple = ()
    condition = "d.status='success'"
    if task_type in ("image", "video"):
        condition += " AND d.task_type=?"
        args = (task_type,)
    if not deduplicate:
        return f"SELECT d.* FROM detections d WHERE {condition}", args
    # 旧记录仅有 saved_name; 新记录哈希可将相同文件的旧分析一并归组。
    return (
        "SELECT * FROM (SELECT d.*, ROW_NUMBER() OVER (PARTITION BY d.task_type, "
        "COALESCE(NULLIF(d.source_hash,''), keys.source_hash, NULLIF(d.saved_name,''), "
        "'record:' || d.id) ORDER BY d.created_at DESC, d.id DESC) AS source_rank "
        "FROM detections d LEFT JOIN (SELECT task_type, saved_name, "
        "MAX(NULLIF(source_hash,'')) AS source_hash FROM detections "
        "WHERE saved_name IS NOT NULL AND saved_name<>'' GROUP BY task_type,saved_name) keys "
        "ON keys.task_type=d.task_type AND keys.saved_name=d.saved_name "
        f"WHERE {condition}) WHERE source_rank=1",
        args,
    )


def stats_class_trend(days: int = 7, granularity: str = "day",
                      deduplicate: bool = False, task_type: str = "",
                      hours: int = 0) -> Dict[str, Any]:
    """类别趋势与主趋势使用相同日/周桶; 缺测桶返回 null 而不是伪造观测。

    Args:
        days: 日 / 周口径的时间跨度, 语义与 `stats_trend` 完全一致。
        granularity: "day" 按自然日分桶 | "week" 按自然周分桶 | "hour" 按小时分桶。
        deduplicate: 同一源素材仅取最新一次成功分析(先全局去重, 再按时间过滤)。
        task_type: 限定 image / video; 空串表示不限。
        hours: 滚动窗口的小时数, 与 `days` 互斥 —— 大于 0 时优先按小时口径,
            窗口是"现在往前 N 小时"而不是自然日 00:00 切分。

    `hours` 之所以不塞进 `days`, 是因为 `days=24` 会被读成"最近 24 天",
    两个口径共用同一参数会让调用方与图表标签都无法区分; 分开传参后
    前端可以明确地给出 `hours=24`, 而后端仍能保持 `days` 分支一字不改。
    """
    if int(hours) > 0:
        return _stats_class_trend_hours(
            max(1, min(int(hours), 168)), deduplicate=deduplicate, task_type=task_type
        )
    days = max(1, min(int(days), 400))
    weekly = str(granularity).lower() == "week"
    today = dt.date.today()
    if weekly:
        start = today - dt.timedelta(days=today.weekday(), weeks=days - 1)
        day_list = [(start + dt.timedelta(weeks=i)).isoformat() for i in range(days)]
        bucket_sql = "date(d.created_at, 'weekday 0', '-6 days')"
    else:
        start = today - dt.timedelta(days=days - 1)
        day_list = [(start + dt.timedelta(days=i)).isoformat() for i in range(days)]
        bucket_sql = "date(d.created_at)"
    selected, args = _analysis_selection(deduplicate, task_type)
    args += (start.isoformat(),)
    connection = get_connection()
    rows = connection.execute(
        f"SELECT {bucket_sql} AS day, cs.class_name, SUM(cs.object_count) AS count "
        f"FROM ({selected}) d JOIN detection_class_stats cs ON cs.detection_id=d.id "
        "WHERE date(d.created_at)>=date(?) GROUP BY day,cs.class_name", args,
    ).fetchall()
    observed = {row["day"] for row in connection.execute(
        f"SELECT DISTINCT {bucket_sql} AS day FROM ({selected}) d "
        "WHERE date(d.created_at)>=date(?)", args,
    ).fetchall()}
    index = {day: i for i, day in enumerate(day_list)}
    series_map: Dict[str, list] = {}
    for row in rows:
        values = series_map.setdefault(row["class_name"], [0 if day in observed else None for day in day_list])
        if row["day"] in index:
            values[index[row["day"]]] = int(row["count"])
    return {
        "days": day_list,
        "labels": [day[5:] + (" 周" if weekly else "") for day in day_list],
        "granularity": "week" if weekly else "day",
        "series": [{"name": name, "data": values} for name, values in sorted(
            series_map.items(), key=lambda item: -sum(value or 0 for value in item[1]))],
    }


def _stats_class_trend_hours(hours: int, deduplicate: bool = False,
                             task_type: str = "") -> Dict[str, Any]:
    """滚动窗口(现在往前 N 小时)的类别趋势, 按整点分桶。

    与日 / 周口径的区别只有两点, 其余(全局去重、缺测桶返回 null、按总量降序)
    完全一致:
      1. 时间过滤用 `d.created_at>=?` 直接比较时间戳, 而不是 `date(...)>=date(?)`。
         后者会把窗口扩到当天 00:00 起, "最近 24 小时"会变成"今天 + 昨天整天"。
      2. 分桶用 `strftime('%Y-%m-%d %H', ...)`, 得到 N 个整点桶; 若仍按天分桶,
         24 小时窗口最多只落到 2 个点上, 曲线会退化成一条竖线。
    """
    hours = max(1, min(int(hours), 168))
    now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    # 以整点对齐: 窗口起点 = 当前整点往前 (N-1) 小时, 正好 N 个完整小时桶
    start_ts = (now - dt.timedelta(hours=hours - 1)).strftime("%Y-%m-%d %H:00:00")
    end_ts = (now + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:00:00")
    hour_list = [
        (now - dt.timedelta(hours=hours - 1 - i)).strftime("%Y-%m-%d %H")
        for i in range(hours)
    ]
    selected, args = _analysis_selection(deduplicate, task_type)
    args += (start_ts, end_ts)
    connection = get_connection()
    rows = connection.execute(
        "SELECT strftime('%Y-%m-%d %H', d.created_at) AS day, cs.class_name, "
        "SUM(cs.object_count) AS count "
        f"FROM ({selected}) d JOIN detection_class_stats cs ON cs.detection_id=d.id "
        "WHERE d.created_at>=? AND d.created_at<? GROUP BY day,cs.class_name", args,
    ).fetchall()
    # 有记录(哪怕该类别 0 框)的桶才画出 0, 完全没有记录的小时保持 null 断层
    observed = {row["day"] for row in connection.execute(
        f"SELECT DISTINCT strftime('%Y-%m-%d %H', d.created_at) AS day FROM ({selected}) d "
        "WHERE d.created_at>=? AND d.created_at<?", args,
    ).fetchall()}
    index = {hour: i for i, hour in enumerate(hour_list)}
    series_map: Dict[str, list] = {}
    for row in rows:
        values = series_map.setdefault(
            row["class_name"], [0 if hour in observed else None for hour in hour_list]
        )
        if row["day"] in index:
            values[index[row["day"]]] = int(row["count"])
    return {
        "days": hour_list,
        "labels": [hour[5:] + " 时" for hour in hour_list],
        "granularity": "hour",
        "hours": hours,
        "series": [{"name": name, "data": values} for name, values in sorted(
            series_map.items(), key=lambda item: -sum(value or 0 for value in item[1]))],
    }


def stats_hourly() -> List[Dict[str, Any]]:
    """全天 24 小时的检测次数分布(全部历史), 用于观察检测活跃时段。"""
    rows = get_connection().execute(
        "SELECT CAST(strftime('%H', created_at) AS INTEGER) AS hour, COUNT(*) AS count "
        "FROM detections GROUP BY hour"
    ).fetchall()
    mapping = {int(r["hour"]): int(r["count"]) for r in rows}
    return [{"hour": h, "count": mapping.get(h, 0)} for h in range(24)]


def stats_classes(days: int = 0, deduplicate: bool = False,
                  task_type: str = "", hours: int = 0) -> List[Dict[str, Any]]:
    """各分类累计目标数量(降序)。

    Args:
        days: 仅统计最近 N 天(自然日口径); 0 或负数表示全部历史。
        deduplicate: 同一源素材仅取最新一次成功分析。
        task_type: 限定 image / video; 空串表示不限。
        hours: 滚动窗口的小时数; 大于 0 时优先于 `days`, 语义是"现在往前 N 小时"。

    `hours` 走时间戳直接比较, 而非 `date(created_at)>=date(?)` —— 自然日口径
    在跨零点时会把窗口往前扩到 00:00, "最近 24 小时"因此变成"今天 + 昨天"。
    """
    selected, args = _analysis_selection(deduplicate, task_type)
    condition = ""
    if hours and int(hours) > 0:
        start_ts = (dt.datetime.now() - dt.timedelta(hours=max(1, min(int(hours), 168)))
                    ).strftime("%Y-%m-%d %H:%M:%S")
        condition = "WHERE d.created_at>=? "
        args += (start_ts,)
    elif days and int(days) > 0:
        condition = "WHERE date(d.created_at)>=date(?) "
        args += ((dt.date.today() - dt.timedelta(days=int(days)-1)).isoformat(),)
    rows = get_connection().execute(
        "SELECT cs.class_name, SUM(cs.object_count) AS count "
        f"FROM ({selected}) d JOIN detection_class_stats cs ON cs.detection_id=d.id "
        f"{condition}GROUP BY cs.class_name ORDER BY count DESC", args,
    ).fetchall()
    total = sum(r["count"] for r in rows) or 1
    return [
        {"class_name": r["class_name"], "count": int(r["count"]),
         "ratio": round(r["count"] / total * 100, 1)}
        for r in rows
    ]


def stats_recent(limit: int = 8) -> List[Dict[str, Any]]:
    """最近的检测记录(用于首页 / 统计页展示)。"""
    limit = max(1, min(int(limit), 50))
    rows = get_connection().execute(
        "SELECT id, task_type, source_name, total_objects, created_at, status, inference_ms "
        "FROM detections ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def stats_type_distribution() -> List[Dict[str, Any]]:
    """图片 / 视频检测次数分布。"""
    rows = get_connection().execute(
        "SELECT task_type, COUNT(*) AS count FROM detections GROUP BY task_type"
    ).fetchall()
    return [{"task_type": r["task_type"], "count": int(r["count"])} for r in rows]


# ---------------------------------------------------- 调查维度: 站点 / 样方


def stats_sites(days: int = 0, task_type: str = "", deduplicate: bool = True
                ) -> List[Dict[str, Any]]:
    """按站点汇总检出力, 并把样方作为第二层下钻返回。

    口径说明:
      - 值取自 `total_objects`, 即该记录"最可用的那个数"(轨迹数 / MaxN / 检出框,
        由任务是否启用跨帧关联决定), 与记录详情页展示的数字同源, 不会出现
        "列表页 12, 站点页 15"这类对不上的情况;
      - 未填写站点的记录归入"未标注站点", 不丢弃 —— 丢弃会让总数小于实际处理量;
      - `deduplicate=True` 时同一源素材只取最新一次成功分析, 与生态指标口径一致。

    Args:
        days: 仅统计最近 N 天; 0 表示全部历史。
        task_type: 限定 image / video; 空串表示不限。
        deduplicate: 是否按源素材去重。
    """
    selected, args = _analysis_selection(deduplicate, task_type)
    condition = ""
    if days and int(days) > 0:
        condition = "WHERE date(d.created_at)>=date(?) "
        args += ((dt.date.today() - dt.timedelta(days=int(days) - 1)).isoformat(),)
    rows = get_connection().execute(
        "SELECT d.site_id, COALESCE(NULLIF(TRIM(d.site),''),'') AS site, "
        "COALESCE(NULLIF(TRIM(d.transect),''),'') AS transect, "
        "COUNT(*) AS records, COALESCE(SUM(d.total_objects),0) AS objects, "
        "COALESCE(MAX(d.maxn_total),0) AS maxn_peak, "
        "COALESCE(SUM(d.total_detections),0) AS detections "
        f"FROM ({selected}) d {condition}"
        "GROUP BY d.site_id, site, transect ORDER BY objects DESC", args,
    ).fetchall()

    # 站点 -> {记录数, 目标数, 样方列表}, 用有序字典思想保持"目标数降序"
    grouped: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        site_name = row["site"] or ""
        # Deleted and pre-map records retain their historical name but must not
        # inherit a newly-created station that happens to reuse that name.
        station_id = row["site_id"]
        key = ("station", station_id) if station_id is not None else ("legacy", site_name)
        node = grouped.setdefault(key, {
            "site_id": station_id,
            "legacy_site": station_id is None,
            "site": site_name or None,
            "site_label": site_name or SITE_UNLABELED,
            "records": 0, "objects": 0, "transects": [],
        })
        node["records"] += int(row["records"])
        node["objects"] += int(row["objects"])
        node["transects"].append({
            "transect": row["transect"] or None,
            "transect_label": row["transect"] or TRANSECT_UNLABELED,
            "records": int(row["records"]),
            "objects": int(row["objects"]),
            "maxn_peak": int(row["maxn_peak"]),
            "detections": int(row["detections"]),
        })
    result = sorted(grouped.values(), key=lambda item: (-item["objects"], item["site_label"]))
    for node in result:          # 样方层同样按目标数降序
        node["transects"].sort(key=lambda item: (-item["objects"], item["transect_label"]))
    return result


def stats_confidence(days: int = 0, bins: int = 10, task_type: str = ""
                     ) -> Dict[str, Any]:
    """置信度分布看板: 全量明细的置信度直方图 + 阈值敏感性。

    只用 `detection_items.confidence` 这一列(每个检出框一行), 因此:
      - 分布反映的是"检出框"的置信度, 不是"记录"的置信度;
      - 阈值敏感性给出"若把置信度阈值提到 x, 还剩多少框", 用于判断阈值是否过松;
      - 视频的采样明细有入库上限(见 MAX_SAMPLE_ITEMS), 因此视频部分是抽样口径,
        在 scope 中明确标注, 避免被误读为全量。
    """
    bins = max(5, min(int(bins), 20))
    day_filter = ""
    args: list = []
    if days and int(days) > 0:
        day_filter = "AND date(d.created_at)>=date(?) "
        args.append((dt.date.today() - dt.timedelta(days=int(days) - 1)).isoformat())
    type_filter = ""
    if task_type in ("image", "video"):
        type_filter = "AND d.task_type=? "
        args.append(task_type)

    conn = get_connection()
    rows = conn.execute(
        "SELECT i.confidence AS conf, d.task_type AS task_type "
        "FROM detection_items i JOIN detections d ON d.id=i.detection_id "
        f"WHERE d.status='success' {day_filter}{type_filter}",
        tuple(args),
    ).fetchall()

    edges = [round(0.01 + i * (0.99 - 0.01) / bins, 4) for i in range(bins + 1)]
    histogram = [{"lower": edges[i], "upper": edges[i + 1], "count": 0} for i in range(bins)]
    total = 0
    conf_list: List[float] = []
    video_boxes = 0
    for row in rows:
        try:
            conf = float(row["conf"])
        except (TypeError, ValueError):
            continue
        total += 1
        conf_list.append(conf)
        if row["task_type"] == "video":
            video_boxes += 1
        idx = int((conf - edges[0]) / ((edges[-1] - edges[0]) / bins))
        idx = min(max(idx, 0), bins - 1)
        histogram[idx]["count"] += 1

    conf_list.sort()

    def _at_least(threshold: float) -> int:
        # 已排序, 用二分定位首个 >= threshold 的位置
        lo, hi = 0, len(conf_list)
        while lo < hi:
            mid = (lo + hi) // 2
            if conf_list[mid] < threshold:
                lo = mid + 1
            else:
                hi = mid
        return len(conf_list) - lo

    thresholds = [0.1, 0.25, 0.5, 0.75, 0.9]
    sensitivity = [
        {"threshold": t, "kept": _at_least(t),
         "ratio": round(_at_least(t) / total * 100, 1) if total else 0.0}
        for t in thresholds
    ]
    median = round(conf_list[len(conf_list) // 2], 3) if conf_list else None
    mean = round(sum(conf_list) / total, 3) if total else None
    return {
        "total": total,
        "bins": histogram,
        "median": median,
        "mean": mean,
        "p10": round(conf_list[int(total * 0.1)], 3) if total else None,
        "p90": round(conf_list[min(int(total * 0.9), total - 1)], 3) if total else None,
        "sensitivity": sensitivity,
        "scope": {
            "days": days, "task_type": task_type or "all", "deduplicate": False,
            "selection": "全部成功检测的明细框; 视频为采样帧明细, 受入库采样上限限制",
            "sampled": video_boxes > 0,
            "video_boxes": video_boxes,
            "limitation": "分布描述的是模型输出置信度, 不等同于人工核验的准确率; "
                          "不同模型的置信度不可直接比较。",
        },
    }


def stats_today(hours: int = 24) -> Dict[str, Any]:
    """今日(或最近 N 小时)检测数据, 作为统计页的第四个时间选项。

    与 `stats_overview.today_*` 的区别: 后者只给两个数, 这里给出构成明细 ——
    分类型、分站点、分时段的今日产出, 以及按口径拆开的计数,
    让"今日 120 个目标"可以直接解释成"图片 90 + 视频 30(其中 MaxN 兜底 30)"。
    """
    hours = max(1, min(int(hours), 168))
    conn = get_connection()
    since = (dt.datetime.now() - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    summary = conn.execute(
        "SELECT COUNT(*) AS records, COALESCE(SUM(total_objects),0) AS objects, "
        "COALESCE(SUM(total_detections),0) AS detections, "
        "COALESCE(SUM(maxn_total),0) AS maxn_sum "
        "FROM detections WHERE created_at >= ? AND status='success'", (since,),
    ).fetchone()

    by_type = conn.execute(
        "SELECT task_type, COUNT(*) AS records, COALESCE(SUM(total_objects),0) AS objects "
        "FROM detections WHERE created_at >= ? AND status='success' "
        "GROUP BY task_type", (since,),
    ).fetchall()

    by_site = conn.execute(
        "SELECT site_id, COALESCE(NULLIF(TRIM(site),''),'') AS site, COUNT(*) AS records, "
        "COALESCE(SUM(total_objects),0) AS objects "
        "FROM detections WHERE created_at >= ? AND status='success' "
        "GROUP BY site_id, site ORDER BY objects DESC", (since,),
    ).fetchall()

    by_hour = conn.execute(
        "SELECT CAST(strftime('%H', created_at) AS INTEGER) AS hour, "
        "COUNT(*) AS records, COALESCE(SUM(total_objects),0) AS objects "
        "FROM detections WHERE created_at >= ? AND status='success' "
        "GROUP BY hour ORDER BY hour", (since,),
    ).fetchall()

    by_source = conn.execute(
        "SELECT counting_mode, COUNT(*) AS records FROM detections "
        "WHERE created_at >= ? AND status='success' GROUP BY counting_mode", (since,),
    ).fetchall()

    return {
        "hours": hours,
        "since": since,
        "records": int(summary["records"] or 0),
        "objects": int(summary["objects"] or 0),
        "detections": int(summary["detections"] or 0),
        "by_type": [{"task_type": r["task_type"], "records": int(r["records"]),
                     "objects": int(r["objects"])} for r in by_type],
        "by_site": [{"site_id": r["site_id"], "legacy_site": r["site_id"] is None,
                     "site": r["site"] or None,
                     "site_label": r["site"] or SITE_UNLABELED,
                     "records": int(r["records"]), "objects": int(r["objects"])}
                    for r in by_site],
        "by_hour": [{"hour": int(r["hour"]), "records": int(r["records"]),
                     "objects": int(r["objects"])} for r in by_hour],
        "by_counting_mode": [{"counting_mode": r["counting_mode"],
                              "records": int(r["records"])} for r in by_source],
        "scope": {
            "window_hours": hours,
            "selection": "按记录创建时间筛选最近 N 小时内的成功检测; 值为记录级 total_objects",
            "limitation": "同一素材的重复检测会被重复计入; 需要去重口径请查看站点维度。",
        },
    }
