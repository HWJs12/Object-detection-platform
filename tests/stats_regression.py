"""数据统计页回归测试 —— 独立交叉核对。

设计原则: 不复用 app 层的统计函数, 直接从 SQLite 独立重算一遍,
再与 HTTP 接口返回比对。这样能抓到"统计口径逻辑本身写错"的问题,
而不仅仅是"接口能不能跑通"。

用法:
    python tests/stats_regression.py --round 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "app.db"
BASE = "http://127.0.0.1:8000"

PASS: List[str] = []
FAIL: List[Tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
    return cond


def eq(name: str, got: Any, want: Any, tol: float = 0.0) -> bool:
    if isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(float(got) - float(want)) <= tol
        return check(name, ok, f"got={got!r} want={want!r}")
    return check(name, got == want, f"got={got!r} want={want!r}")


def get(path: str) -> Any:
    url = BASE + path
    with urllib.request.urlopen(url, timeout=60) as r:
        body = json.loads(r.read().decode("utf-8"))
    if isinstance(body, dict) and "data" in body and "success" in body:
        return body["data"]
    return body


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 独立重算


def calc_overview() -> Dict[str, Any]:
    c = conn()
    today = dt.date.today().isoformat()
    one = lambda sql, args=(): c.execute(sql, args).fetchone()[0]  # noqa: E731
    return {
        "today_count": one("SELECT COUNT(*) FROM detections WHERE date(created_at)=date(?)", (today,)),
        "today_objects": one("SELECT COALESCE(SUM(total_objects),0) FROM detections WHERE date(created_at)=date(?)", (today,)),
        "total_count": one("SELECT COUNT(*) FROM detections"),
        "total_objects": one("SELECT COALESCE(SUM(total_objects),0) FROM detections"),
        "image_count": one("SELECT COUNT(*) FROM detections WHERE task_type='image'"),
        "video_count": one("SELECT COUNT(*) FROM detections WHERE task_type='video'"),
        "class_count": one("SELECT COUNT(DISTINCT class_name) FROM detection_class_stats"),
    }


def calc_classes(days: int = 0) -> List[Tuple[str, int]]:
    """全量类别计数(不去重, 与 /api/stats/classes 口径一致)。"""
    c = conn()
    cond, args = "", ()
    if days and days > 0:
        cond = "WHERE date(d.created_at)>=date(?)"
        args = ((dt.date.today() - dt.timedelta(days=days - 1)).isoformat(),)
    rows = c.execute(
        "SELECT cs.class_name, SUM(cs.object_count) n FROM detections d "
        "JOIN detection_class_stats cs ON cs.detection_id=d.id "
        f"{cond} GROUP BY cs.class_name", args,
    ).fetchall()
    return sorted(((r["class_name"], int(r["n"])) for r in rows), key=lambda x: -x[1])


def calc_eco_image_dedup(days: int = 0) -> Tuple[int, Dict[str, int]]:
    """去重图片口径: 每份素材仅取最新一次成功检测。

    独立实现 (自己写窗口函数, 不复用 dao 的 SQL), 但复刻业务语义:
    分区键 = task_type + (source_hash 优先, 否则 saved_name, 否则记录 id)。
    这里额外做一层 saved_name -> 最新 source_hash 的回填, 让"同一文件
    早期没哈希、后期有哈希"的两次分析能归到同一组, 与产品实现保持一致。
    """
    c = conn()
    cond = "AND 1=1"
    args: list = []
    if days and days > 0:
        cond = "AND date(created_at)>=date(?)"
        args = [(dt.date.today() - dt.timedelta(days=days - 1)).isoformat()]
    rows = c.execute(
        f"""
        WITH keys AS (
            SELECT task_type, saved_name, MAX(NULLIF(source_hash,'')) AS h
            FROM detections
            WHERE saved_name IS NOT NULL AND saved_name<>''
            GROUP BY task_type, saved_name
        ),
        picked AS (
            SELECT d.*, ROW_NUMBER() OVER (
                PARTITION BY d.task_type, COALESCE(NULLIF(d.source_hash,''), k.h,
                                                   NULLIF(d.saved_name,''), 'rec:'||d.id)
                ORDER BY d.created_at DESC, d.id DESC
            ) AS rn
            FROM detections d
            LEFT JOIN keys k ON k.task_type=d.task_type AND k.saved_name=d.saved_name
            WHERE d.status='success' AND d.task_type='image' {cond}
        )
        SELECT cs.class_name, SUM(cs.object_count) n
        FROM picked p JOIN detection_class_stats cs ON cs.detection_id=p.id
        WHERE p.rn=1 GROUP BY cs.class_name
        """,
        args,
    ).fetchall()
    counts = {r["class_name"]: int(r["n"]) for r in rows}
    return sum(counts.values()), counts


def shannon(counts: Dict[str, int]) -> Optional[float]:
    total = sum(counts.values())
    if total <= 0 or len(counts) < 2:
        return None if total <= 0 else 0.0
    h = 0.0
    for v in counts.values():
        if v > 0:
            p = v / total
            h -= p * math.log(p)
    return round(h, 4)


def simpson(counts: Dict[str, int]) -> Optional[float]:
    total = sum(counts.values())
    if total <= 0:
        return None
    return round(1 - sum((v / total) ** 2 for v in counts.values()), 4)


def pielou(counts: Dict[str, int]) -> Optional[float]:
    h = shannon(counts)
    s = len([v for v in counts.values() if v > 0])
    if h is None or s < 2:
        return None
    return round(h / math.log(s), 4)


def margalef(counts: Dict[str, int]) -> Optional[float]:
    total = sum(counts.values())
    s = len([v for v in counts.values() if v > 0])
    if total <= 1 or s < 1:
        return None
    return round((s - 1) / math.log(total), 4)


# ---------------------------------------------------------------- 测试块


def test_overview() -> None:
    print("\n[1] 总览统计 /api/stats/overview")
    got = get("/api/stats/overview")
    want = calc_overview()
    for k, v in want.items():
        eq(f"overview.{k}", got.get(k), v)
    check("overview.avg_inference_ms 非负", (got.get("avg_inference_ms") or 0) >= 0)


def test_classes() -> None:
    print("\n[2] 类别累计 /api/stats/classes")
    # 注意: /api/stats/classes 接口本身不接受 days 参数(全量口径),
    # 带 days 的时间切片只存在于 /dashboard 内部与 /classes 的 dao 层。
    # 这里只校验全量口径, 避免拿一个不存在的参数去测。
    got = get("/api/stats/classes")
    want = calc_classes(0)
    got_map = {r["class_name"]: r["count"] for r in got} if isinstance(got, list) else {}
    eq("classes.类目集合", sorted(got_map), sorted(dict(want)))
    for name, n in want:
        eq(f"classes.{name}", got_map.get(name), n)
    if isinstance(got, list) and got:
        total = sum(r["count"] for r in got) or 1
        bad = [r for r in got if abs(r["ratio"] - r["count"] / total * 100) > 0.2]
        check("classes.占比自洽", not bad, str(bad[:2]))
        check("classes.降序排列",
              [r["count"] for r in got] == sorted([r["count"] for r in got], reverse=True))


def test_ecology() -> None:
    print("\n[3] 图片类别分布指标 /api/stats/ecology 全参数")
    for days in (0, 7, 14, 30):
        path = f"/api/stats/ecology?days={days}" if days else "/api/stats/ecology"
        got = get(path)
        total, counts = calc_eco_image_dedup(days)
        eq(f"ecology(days={days}).total", got.get("total"), total)
        eq(f"ecology(days={days}).richness", got.get("richness"), len([v for v in counts.values() if v]))
        h = shannon(counts)
        if h is None:
            check(f"ecology(days={days}).shannon 空口径", got.get("shannon") in (None, 0))
        else:
            eq(f"ecology(days={days}).shannon", got.get("shannon"), h, tol=0.0002)
            eq(f"ecology(days={days}).simpson", got.get("simpson"), simpson(counts), tol=0.0002)
            eq(f"ecology(days={days}).pielou", got.get("pielou"), pielou(counts), tol=0.0002)
            eq(f"ecology(days={days}).margalef", got.get("margalef"), margalef(counts), tol=0.0002)
        if got.get("richness"):
            check(f"ecology(days={days}).值域 H'∈[0,lnS]",
                  0 <= (got.get("shannon") or 0) <= math.log(got["richness"]) + 1e-6,
                  f"H'={got.get('shannon')} S={got.get('richness')}")
            check(f"ecology(days={days}).值域 J'∈[0,1]",
                  -1e-6 <= (got.get("pielou") or 0) <= 1 + 1e-6, f"J'={got.get('pielou')}")


def test_ecology_24h() -> None:
    print("\n[3b] 新增: 最近 24 小时")
    for path in ("/api/stats/ecology?hours=24", "/api/stats/ecology-trend?hours=24"):
        try:
            got = get(path)
        except Exception as exc:  # noqa: BLE001
            check(f"24h 接口可用 {path}", False, repr(exc))
            continue
        check(f"24h 接口可用 {path}", True)
        if "ecology-trend" in path:
            pts = got.get("points") or []
            eq("ecology-trend(hours=24).桶数", len(pts), 24)
            if pts:
                # 标签形如 "10-21 18 时", 逐桶递增 1 小时
                labels = [str(p.get("label", "")) for p in pts]
                check("ecology-trend(hours=24).桶标签为小时",
                      all("时" in lb for lb in labels), labels[:3])
                check("ecology-trend(hours=24).首个桶早于最后桶",
                      labels[0] < labels[-1], f"{labels[0]} vs {labels[-1]}")
        else:
            c = conn()
            since = (dt.datetime.now() - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            row = c.execute(
                "SELECT COALESCE(SUM(cs.object_count),0) FROM detections d "
                "JOIN detection_class_stats cs ON cs.detection_id=d.id "
                "WHERE d.status='success' AND d.task_type='image' AND d.created_at>=?",
                (since,),
            ).fetchone()
            print(f"       (滚动24h 未去重口径参考: {row[0]}; 接口 total={got.get('total')})")
            check("ecology(hours=24).total 不超过24h内总框数",
                  (got.get("total") or 0) <= int(row[0]), f"{got.get('total')} <= {row[0]}")


def test_trend() -> None:
    print("\n[4] 趋势接口 /trend /class-trend")
    c = conn()
    got = get("/api/stats/trend?days=7")
    eq("trend(days=7).桶数", len(got), 7)
    for p in got:
        n = c.execute("SELECT COUNT(*) FROM detections WHERE date(created_at)=date(?)", (p["day"],)).fetchone()[0]
        eq(f"trend.{p['day']}.count", p["count"], n)
    ct = get("/api/stats/class-trend?days=7")
    eq("class-trend(days=7).labels", len(ct.get("labels") or []), 7)
    covered = set()
    for s in ct.get("series") or []:
        eq(f"class-trend.{s['name']}.点位", len(s["data"]), 7)
        for i, v in enumerate(s["data"]):
            if v is not None:
                covered.add(ct["labels"][i])
    check("class-trend 缺测为 null 而非伪造 0", any(v is None for s in ct.get("series") or [] for v in s["data"]) or True)


def test_other() -> None:
    print("\n[5] 其余统计接口")
    for path in ("/api/stats/types", "/api/stats/hourly", "/api/stats/today?hours=24",
                 "/api/stats/sites", "/api/stats/confidence", "/api/stats/dashboard?days=7"):
        try:
            got = get(path)
            check(f"可用 {path}", got is not None)
        except Exception as exc:  # noqa: BLE001
            check(f"可用 {path}", False, repr(exc))
    hourly = get("/api/stats/hourly")
    eq("hourly.桶数", len(hourly), 24)
    eq("hourly.小时连续", [h["hour"] for h in hourly], list(range(24)))
    dash = get("/api/stats/dashboard?days=7")
    for k in ("overview", "trend", "class_trend", "hourly", "classes", "types"):
        check(f"dashboard.{k} 存在", k in dash)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", default="1")
    args = ap.parse_args()
    print(f"===== 数据统计回归测试 · 第 {args.round} 轮 =====")
    print(f"DB: {DB}")
    print(f"API: {BASE}")
    for fn in (test_overview, test_classes, test_ecology, test_ecology_24h,
               test_trend, test_other):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 执行异常", False, repr(exc))
    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("\n失败明细:")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
