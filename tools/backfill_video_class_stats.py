"""回填视频记录的类别明细口径 (一次性数据修复)。

背景:
    追踪部分失效 (tracking_partial) 的视频记录, detection_class_stats 存的是
    逐帧累计框数 (例如合计 2332), 而主记录 total_objects 存的是 MaxN (10)。
    统计页按类别汇总 class_stats 时数字被放大数百倍。

规则:
    - 主记录 total_objects == total_detections (旧代码直接存累计框): 全面修正,
      total_objects -> maxn_total, 回填 maxn_per_class / maxn_total / cpunx,
      重写 class_stats。
    - 主记录 total_objects == maxn_total (新代码但明细口径错, 如 4310/4311):
      仅重写 class_stats。
    - total_objects == unique_objects > 0 (旧版追踪成功记录): 口径本就一致, 跳过。

用法:
    python tools/backfill_video_class_stats.py --dry-run   # 只打印不动库
    python tools/backfill_video_class_stats.py --apply     # 实际写入
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "app.db"


def maxn_from_frames(conn: sqlite3.Connection, rid: int) -> dict:
    """从逐帧统计重建每个类别的单帧峰值。"""
    rows = conn.execute(
        "SELECT class_counts FROM video_frame_stats WHERE detection_id=?", (rid,)
    ).fetchall()
    maxn: dict = {}
    for (cc,) in rows:
        if not cc:
            continue
        try:
            per = json.loads(cc)
        except (json.JSONDecodeError, TypeError):
            continue
        for name, n in per.items():
            if isinstance(n, (int, float)) and n > maxn.get(name, 0):
                maxn[name] = int(n)
    return maxn


def maxn_from_items(conn: sqlite3.Connection, rid: int) -> dict:
    """兜底: 帧统计缺类别明细时, 用 detection_items 采样按帧重建。

    采样明细有上限 (MAX_SAMPLE_ITEMS), 重建结果是真实 MaxN 的下界,
    仅用于旧代码写出的空 class_counts 记录 (如 #4301)。
    """
    rows = conn.execute(
        "SELECT frame_index, class_name, COUNT(*) c FROM detection_items "
        "WHERE detection_id=? GROUP BY frame_index, class_name",
        (rid,),
    ).fetchall()
    maxn: dict = {}
    for r in rows:
        if r["c"] > maxn.get(r["class_name"], 0):
            maxn[r["class_name"]] = int(r["c"])
    return maxn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, source_name, created_at, total_objects, unique_objects, "
        "total_detections, maxn_total, maxn_per_class, counting_mode, unique_count_valid "
        "FROM detections WHERE task_type='video' AND status='success' ORDER BY id"
    ).fetchall()

    changed, skipped, failed = 0, 0, 0
    for r in rows:
        rid = r["id"]
        if r["unique_count_valid"]:
            skipped += 1
            continue
        maxn = maxn_from_frames(conn, rid)
        if not maxn:
            maxn = maxn_from_items(conn, rid)
            if maxn:
                print(f"  注意 #{rid}: 帧统计无类别明细, 改用 detection_items 采样重建 (结果为下界)")
        if not maxn:
            print(f"  跳过 #{rid} {r['source_name']}: 帧统计与明细采样均为空, 无法重建")
            skipped += 1
            continue
        maxn_total = max(maxn.values())
        tot_obj = r["total_objects"] or 0
        tot_det = r["total_detections"] or 0

        current_stats = conn.execute(
            "SELECT class_name, class_id, object_count FROM detection_class_stats WHERE detection_id=?",
            (rid,),
        ).fetchall()
        current_total = sum(x["object_count"] for x in current_stats)
        id_of = {x["class_name"]: x["class_id"] for x in current_stats}

        need_stats_fix = current_total != sum(maxn.values())
        need_main_fix = (tot_obj == tot_det and tot_det != maxn_total)

        if not need_stats_fix and not need_main_fix:
            skipped += 1
            continue

        print(f"  修复 #{rid} {r['source_name']} ({r['created_at']}, {r['counting_mode']}):")
        print(f"    class_stats 合计 {current_total} -> {sum(maxn.values())} {dict(sorted(maxn.items(), key=lambda kv: -kv[1]))}")
        if need_main_fix:
            print(f"    total_objects {tot_obj} -> {maxn_total} (原=逐帧累计 {tot_det})")

        if apply:
            duration_sec = conn.execute(
                "SELECT duration_sec FROM detections WHERE id=?", (rid,)).fetchone()[0]
            cpunx = round(maxn_total / (duration_sec / 3600.0), 1) \
                if duration_sec and duration_sec > 0 and maxn_total else None
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM detection_class_stats WHERE detection_id=?", (rid,))
                conn.executemany(
                    "INSERT INTO detection_class_stats (detection_id, class_id, class_name, object_count) "
                    "VALUES (?,?,?,?)",
                    [(rid, id_of.get(name, -1), name, n) for name, n in maxn.items()],
                )
                conn.execute(
                    "UPDATE detections SET total_objects=?, maxn_total=?, maxn_per_class=?, cpunx=? "
                    "WHERE id=?",
                    (maxn_total, maxn_total, json.dumps(maxn, ensure_ascii=False), cpunx, rid),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                failed += 1
                print(f"    !! 写入失败, 已回滚")
                continue
        changed += 1

    print(f"\n需修复 {changed} 条, 跳过 {skipped} 条, 失败 {failed} 条"
          + ("" if apply else "  (dry-run, 未写库)"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
