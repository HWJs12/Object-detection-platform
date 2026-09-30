"""数据访问层(DAO): 所有 SQL 均在此集中, 一律使用参数化查询防注入。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.db.database import get_connection, transaction

# ---------------------------------------------------------------- 写入


def insert_detection(data: Dict[str, Any]) -> int:
    """写入一条检测主记录, 返回自增 id。"""
    keys = [
        "task_type", "source_name", "saved_name", "result_image", "created_at",
        "total_objects", "image_width", "image_height", "inference_ms",
        "frames_processed", "avg_fps", "duration_sec", "conf_threshold",
        "status", "message",
    ]
    cols = [k for k in keys if k in data]
    sql = f"INSERT INTO detections ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})"
    with transaction() as cur:
        cur.execute(sql, [data[c] for c in cols])
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


def insert_frame_stats(detection_id: int, rows: Sequence[Tuple[int, float, int]]) -> None:
    """写入视频逐帧统计: rows = [(frame_index, timestamp_sec, total_objects), ...]。"""
    if not rows:
        return
    sql = (
        "INSERT INTO video_frame_stats (detection_id, frame_index, timestamp_sec, total_objects) "
        "VALUES (?, ?, ?, ?)"
    )
    with transaction() as cur:
        cur.executemany(sql, [(detection_id, f, t, c) for f, t, c in rows])


def delete_record(detection_id: int) -> bool:
    """删除一条记录(级联删除明细), 返回是否删除成功。"""
    with transaction() as cur:
        cur.execute("DELETE FROM detections WHERE id = ?", (detection_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- 查询


def get_record(detection_id: int) -> Optional[Dict[str, Any]]:
    row = get_connection().execute(
        "SELECT * FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()
    return dict(row) if row else None


def get_record_items(detection_id: int, limit: int = 500) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT * FROM detection_items WHERE detection_id = ? "
        "ORDER BY frame_index, confidence DESC LIMIT ?",
        (detection_id, limit),
    ).fetchall()
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
        "SELECT frame_index, timestamp_sec, total_objects FROM video_frame_stats "
        "WHERE detection_id = ? ORDER BY frame_index LIMIT ?",
        (detection_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_records(task_type: str = "", keyword: str = "", date_from: str = "",
                 date_to: str = "", page: int = 1, page_size: int = 10
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """分页查询检测记录, 支持类型 / 文件名关键字 / 日期区间过滤。"""
    where, args = ["1=1"], []

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
    return [dict(r) for r in rows], int(total)


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


def stats_trend(days: int = 7) -> List[Dict[str, Any]]:
    """最近 N 天每日检测次数与目标数(缺失日期补 0)。"""
    days = max(1, min(int(days), 60))
    today = dt.date.today()
    start = (today - dt.timedelta(days=days - 1)).isoformat()

    rows = get_connection().execute(
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
        result.append({"day": day, "count": int(item["count"]), "objects": int(item["objects"])})
    return result


def stats_classes() -> List[Dict[str, Any]]:
    """各分类累计目标数量(降序)。"""
    rows = get_connection().execute(
        "SELECT class_name, SUM(object_count) AS count FROM detection_class_stats "
        "GROUP BY class_name ORDER BY count DESC"
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
        "SELECT id, task_type, source_name, total_objects, created_at, status "
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
