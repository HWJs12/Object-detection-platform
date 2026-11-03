"""本轮改造的回归测试: MaxN 口径兜底 / 调查维度 / 站点与置信度看板 / 报告附录。

覆盖点:
  1. normalize_survey 的边界(空白、超长、非法水深);
  2. _effective_count 的三档优先级, 确保不再产生"不可用"空位;
  3. stats_sites 的"未标注站点"归并、样方下钻结构、去重口径;
  4. stats_confidence 的直方图完备性与阈值敏感性单调性;
  5. stats_today 的窗口筛选与按口径分组;
  6. 报告方法学附录的关键断言(口径定义、边界说明、公式不使用缺失字形);
  7. 旧库迁移时 site 索引不阻塞启动。
"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import report
from app.db import dao, database
from app.utils.survey import normalize_survey
from config import settings


class SurveyNormalizationTests(unittest.TestCase):
    def test_text_fields_are_trimmed_and_truncated(self):
        result = normalize_survey({
            "site": "  西岛东侧  ", "transect": "T" * 100,
            "survey_round": "R" * 80, "depth_m": "6.55",
        })
        self.assertEqual(result["site"], "西岛东侧")
        self.assertEqual(len(result["transect"]), 64)
        self.assertEqual(len(result["survey_round"]), 32)
        self.assertEqual(result["depth_m"], 6.55)

    def test_invalid_depth_becomes_none_not_zero(self):
        # 0 / 负数 / 非数字 / 无穷都必须视为"未提供", 否则会把站点均值拉低
        for bad in (0, -3, "abc", float("inf"), None, ""):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize_survey({"depth_m": bad})["depth_m"])

    def test_missing_survey_yields_empty_not_garbage(self):
        self.assertEqual(normalize_survey(None),
                         {"site": "", "transect": "", "survey_round": "", "depth_m": None})


class EffectiveCountTests(unittest.TestCase):
    """三档计数优先级: 轨迹数 -> MaxN -> 累计检出框, 任何情况都不返回 null。"""

    def test_priority_chain(self):
        base = {"task_type": "video", "total_objects": 0}
        self.assertEqual(dao._effective_count(
            {**base, "unique_count_valid": True, "unique_objects": 5,
             "maxn_total": 9, "total_detections": 20}), 5)
        self.assertEqual(dao._effective_count(
            {**base, "unique_count_valid": False, "unique_objects": None,
             "maxn_total": 9, "total_detections": 20}), 9)
        self.assertEqual(dao._effective_count(
            {**base, "unique_count_valid": False, "unique_objects": None,
             "maxn_total": 0, "total_detections": 20}), 20)

    def test_image_uses_object_count(self):
        self.assertEqual(dao._effective_count(
            {"task_type": "image", "total_objects": 12}), 12)

    def test_source_labels_are_exhaustive(self):
        cases = [
            ({"task_type": "image", "total_objects": 3}, "image_objects"),
            ({"task_type": "video", "unique_count_valid": True, "unique_objects": 1,
              "maxn_total": 2}, "unique_tracks"),
            ({"task_type": "video", "unique_count_valid": False, "maxn_total": 2},
             "maxn"),
            ({"task_type": "video", "unique_count_valid": False, "maxn_total": 0},
             "total_detections"),
        ]
        for record, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(dao._effective_count_source(record), expected)

    def test_maxn_summary_marks_unavailable_without_fake_numbers(self):
        summary = dao._maxn_summary({
            "task_type": "video", "maxn_per_class": {}, "maxn_total": 0,
            "duration_sec": 60, "cpunx": None,
        })
        self.assertFalse(summary["available"])
        self.assertIsNone(summary["cpunx"])
        self.assertEqual(summary["maxn_total"], 0)

    def test_maxn_summary_backfills_cpunx_from_duration(self):
        summary = dao._maxn_summary({
            "task_type": "video", "maxn_per_class": {"海胆": 6}, "maxn_total": 6,
            "duration_sec": 3600, "cpunx": None,
        })
        self.assertTrue(summary["available"])
        self.assertEqual(summary["cpunx"], 6.0)


class StatsBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="underwater_board_test_")
        self.previous_path = settings.db_path
        self.previous_conn = getattr(database._local, "conn", None)
        database._local.conn = None
        settings.db_path = Path(self.temp.name) / "app.sqlite"
        with mock.patch.object(database, "_register_exit_hook"):
            database.init_db()

    def tearDown(self):
        database.close_connection()
        settings.db_path = self.previous_path
        database._local.conn = self.previous_conn
        self.temp.cleanup()

    def record(self, **overrides):
        record = {
            "task_type": "image", "source_name": "a.jpg", "saved_name": "a.jpg",
            "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_objects": 4, "unique_objects": 4, "total_detections": 4,
            "counting_mode": "image_objects", "unique_count_valid": True,
            "status": "success",
        }
        record.update(overrides)
        return record

    def test_sites_group_unlabeled_records_instead_of_dropping(self):
        dao.save_detection(self.record(site="西岛东侧", transect="T1", total_objects=5), [], [])
        dao.save_detection(self.record(site="西岛东侧", transect="T2", total_objects=3), [], [])
        dao.save_detection(self.record(source_name="b.jpg", total_objects=7), [], [])
        rows = dao.stats_sites(0, "", False)
        labels = {r["site_label"]: r for r in rows}
        self.assertIn("未标注站点", labels)
        self.assertEqual(labels["未标注站点"]["objects"], 7)
        self.assertEqual(labels["西岛东侧"]["objects"], 8)
        # 样方作为第二层, 按目标数降序
        transects = labels["西岛东侧"]["transects"]
        self.assertEqual([t["transect"] for t in transects], ["T1", "T2"])
        self.assertEqual(sum(r["objects"] for r in rows), 15)

    def test_sites_dedup_keeps_latest_analysis_per_source(self):
        today = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dao.save_detection(self.record(site="A", source_hash="h" * 64, total_objects=10,
                                       created_at="2026-01-01 10:00:00"), [], [])
        dao.save_detection(self.record(site="A", source_hash="h" * 64, total_objects=2,
                                       created_at=today), [], [])
        dedup = dao.stats_sites(0, "", True)
        raw = dao.stats_sites(0, "", False)
        self.assertEqual(sum(r["objects"] for r in dedup), 2)
        self.assertEqual(sum(r["objects"] for r in raw), 12)

    def test_confidence_bins_cover_all_boxes_and_sensitivity_is_monotonic(self):
        items = [{"class_id": 0, "class_name": "urchin", "confidence": c, "bbox": [1, 2, 3, 4]}
                 for c in (0.05, 0.3, 0.55, 0.8, 0.95)]
        dao.save_detection(self.record(total_objects=5), items, [(0, "urchin", 5)])
        data = dao.stats_confidence(0, 10, "")
        self.assertEqual(data["total"], 5)
        self.assertEqual(sum(b["count"] for b in data["bins"]), 5)
        self.assertEqual(len(data["bins"]), 10)
        # 桶区间必须连续且升序, 否则前端画出来的直方图会有空隙
        for lower, upper in zip(data["bins"], data["bins"][1:]):
            self.assertAlmostEqual(lower["upper"], upper["lower"], places=6)
        kept = [s["kept"] for s in data["sensitivity"]]
        self.assertEqual(kept, sorted(kept, reverse=True))
        # 敏感性阈值从 0.1 起算(dao.stats_confidence 的 thresholds 首档),
        # 置信度 0.05 的框低于最低档, 不计入 >=0.1, 故为 4 而非 5
        self.assertEqual([s["threshold"] for s in data["sensitivity"]][0], 0.1)
        self.assertEqual(kept[0], 4)
        self.assertEqual(data["median"], 0.55)

    def test_today_window_only_counts_recent_records(self):
        dao.save_detection(self.record(total_objects=6), [], [])
        old = (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        dao.save_detection(self.record(source_name="old.jpg", total_objects=99,
                                       created_at=old), [], [])
        today = dao.stats_today(24)
        self.assertEqual(today["records"], 1)
        self.assertEqual(today["objects"], 6)
        self.assertEqual(today["by_site"][0]["site_label"], "未标注站点")
        self.assertEqual(today["by_counting_mode"][0]["counting_mode"], "image_objects")

    def test_migration_adds_site_index_without_blocking_startup(self):
        conn = database.get_connection()
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("idx_detections_site", indexes)


class ReportAppendixTests(unittest.TestCase):
    def _items(self, **overrides):
        item = {
            "source_name": "a.jpg", "total": 2, "created_at": "2026-10-21 10:00:00",
            "conf_threshold": 0.25, "source_hash": "a" * 64,
            "counting_mode": "image_objects", "maxn_total": 0,
            "counts": [{"class_name": "海胆", "count": 2}],
            "detections": [{"class_name": "海胆", "confidence": 0.9, "bbox": [1, 2, 30, 40]},
                           {"class_name": "海胆", "confidence": 0.5, "bbox": [7, 8, 20, 40]}],
            "inference_snapshot": {"model": {"name": "best.pt", "sha256": "b" * 64},
                                   "device": "cpu", "parameters": {"imgsz": 640}},
            "site": "西岛东侧", "transect": "T1", "depth_m": 6.5, "survey_round": "2026春",
        }
        item.update(overrides)
        return [item]

    def _text(self, items):
        payload = report.build_report_pdf(items, {"batch_id": "B-1"})
        pypdf = __import__("pypdf")
        reader = pypdf.PdfReader(__import__("io").BytesIO(payload))
        return "\n".join((p.extract_text() or "") for p in reader.pages)

    def test_appendix_contains_calibers_and_boundaries(self):
        text = self._text(self._items())
        self.assertIn("附录 A · 方法学说明", text)
        for section in ("A.1 数据来源与复现要素", "A.2 调查维度", "A.3 计数口径",
                        "A.4 指标定义", "A.5 适用边界与已知局限"):
            self.assertIn(section, text)
        self.assertIn("单帧峰值 MaxN", text)
        self.assertIn("CPUnx", text)
        self.assertIn("西岛东侧", text)
        # 公式不使用 Unicode 下标(中文字体缺字形会渲染成空白)
        self.assertIn("p_i", text)
        self.assertNotIn("\u1d62", text)

    def test_appendix_reports_missing_survey_honestly(self):
        text = self._text(self._items(site="", transect="", depth_m=None, survey_round=""))
        self.assertIn("未标注", text)
        self.assertIn("这部分不计入站点间比较", text)

    def test_appendix_states_batch_caliber_distribution(self):
        text = self._text(self._items(counting_mode="tracking_partial", maxn_total=7))
        self.assertIn("本批实际采用的口径", text)
        self.assertIn("单帧峰值 MaxN 1 张", text)


if __name__ == "__main__":
    unittest.main()
