"""Statistics contract checks; no user records or model weights are touched."""
from __future__ import annotations

import datetime as dt
import math
import unittest
from unittest.mock import DEFAULT, patch

from app.api import stats
from app.core.ecology import build_payload, compute_indicators


class DistributionTests(unittest.TestCase):
    def test_balanced_model_categories_have_normalized_description(self):
        result = build_payload([{"class_name": str(i), "count": 2} for i in range(4)])
        self.assertAlmostEqual(result["shannon"], math.log(4), places=4)
        self.assertEqual(result["pielou"], 1.0)
        self.assertEqual(result["shannon_max"], round(math.log(4), 4))
        self.assertEqual(result["interpretation"]["level"], "descriptive")
        self.assertIn("100.0%", result["interpretation"]["evenness_text"])
        self.assertNotIn("群落", result["interpretation"]["shannon_text"])

    def test_duplicate_category_names_are_merged_before_richness(self):
        result = compute_indicators([
            {"class_name": "fish", "count": 2}, {"class_name": "fish", "count": 3},
            {"class_name": "invalid", "count": float("inf")},
        ])
        self.assertEqual(result["richness"], 1)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["shannon"], 0)
        self.assertIsNone(result["pielou"])

    def test_empty_observation_does_not_invent_zero_diversity(self):
        result = compute_indicators([])
        self.assertIsNone(result["shannon"])
        self.assertIsNone(result["pielou"])


class QueryDefaultRobustnessTests(unittest.IsolatedAsyncioTestCase):
    """路由函数被直接以 Python 调用(而非 HTTP)时, Query(...) 默认值会原样传入。

    一旦函数内部对这些参数直接做 int(), 就会抛
    "TypeError: int() argument must be ..., not 'Query'"。
    这组用例锁住"直接调用也要能用"这条底线。
    """

    async def test_ecology_direct_call_omitting_hours_does_not_raise(self):
        with patch.object(stats.dao, "stats_classes", return_value=[]):
            response = await stats.ecology(days=14)
        self.assertEqual(response["data"]["scope"]["days"], 14)

    async def test_ecology_direct_call_with_no_arguments_does_not_raise(self):
        with patch.object(stats.dao, "stats_classes", return_value=[]):
            response = await stats.ecology()
        self.assertEqual(response["data"]["scope"]["days"], 0)

    async def test_ecology_trend_direct_call_omitting_hours_does_not_raise(self):
        with patch.object(stats.dao, "stats_class_trend", return_value={
            "labels": ["10-01"], "series": [], "granularity": "day",
        }):
            response = await stats.ecology_trend(days=14)
        self.assertEqual(response["data"]["scope"]["granularity"], "day")


class StatisticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ecology_uses_latest_image_analysis_only(self):
        with patch.object(stats.dao, "stats_classes", return_value=[]) as classes:
            # hours 是新增的滚动窗口参数, 自然日口径下必须为 0(不参与过滤)
            response = await stats.ecology(days=14, hours=0)
        classes.assert_called_once_with(days=14, deduplicate=True, task_type="image", hours=0)
        self.assertEqual(response["data"]["scope"]["task_type"], "image")
        self.assertTrue(response["data"]["scope"]["deduplicate"])

    async def test_ecology_trend_uses_same_selection_and_keeps_missing_dates_null(self):
        with patch.object(stats.dao, "stats_class_trend", return_value={
            "labels": ["10-01", "10-02", "10-03"],
            "series": [{"name": "fish", "data": [2, None, 3]}],
        }) as trend:
            response = await stats.ecology_trend(days=3, hours=0)
        trend.assert_called_once_with(3, "day", deduplicate=True, task_type="image", hours=0)
        self.assertIsNone(response["data"]["points"][1]["shannon"])

    async def test_ecology_hours_uses_rolling_window_not_days(self):
        # 「最近 24 小时」必须走 hours=24 的滚动窗口, 不能落到 days=24(那是 24 天)
        with patch.object(stats.dao, "stats_classes", return_value=[]) as classes:
            await stats.ecology(days=0, hours=24)
        classes.assert_called_once_with(days=0, deduplicate=True, task_type="image", hours=24)

    async def test_ecology_trend_hours_buckets_by_hour(self):
        with patch.object(stats.dao, "stats_class_trend", return_value={
            "labels": ["10-22 08 时"], "series": [], "granularity": "hour",
        }) as trend:
            response = await stats.ecology_trend(days=14, hours=24)
        trend.assert_called_once_with(14, "day", deduplicate=True, task_type="image", hours=24)
        self.assertEqual(response["data"]["scope"]["granularity"], "hour")
        self.assertEqual(response["data"]["scope"]["hours"], 24)

    async def test_weekly_dashboard_uses_weekly_category_buckets(self):
        names = ("stats_overview", "stats_trend", "stats_class_trend", "stats_hourly",
                 "stats_classes", "stats_type_distribution", "stats_recent",
                 # 新增看板: 今日构成 / 站点维度 / 置信度分布
                 "stats_today", "stats_sites", "stats_confidence")
        with patch.multiple(stats.dao, **{name: DEFAULT for name in names}) as mocks:
            for mock in mocks.values():
                mock.return_value = []
            await stats.dashboard(days=8, granularity="week")
            mocks["stats_trend"].assert_called_once_with(8, "week")
            mocks["stats_class_trend"].assert_called_once_with(8, "week")
            # Eight weekly buckets include seven full weeks and the current partial week.
            today = dt.date.today()
            first_monday = today - dt.timedelta(days=today.weekday(), weeks=7)
            window_days = (today - first_monday).days + 1
            mocks["stats_classes"].assert_called_once_with(window_days)
            mocks["stats_sites"].assert_called_once_with(window_days, "", True)
            mocks["stats_confidence"].assert_called_once_with(window_days, 10, "")

    async def test_dashboard_days_zero_falls_back_to_weekly_trend_axis(self):
        names = ("stats_overview", "stats_trend", "stats_class_trend", "stats_hourly",
                 "stats_classes", "stats_type_distribution", "stats_recent",
                 "stats_today", "stats_sites", "stats_confidence")
        with patch.multiple(stats.dao, **{name: DEFAULT for name in names}) as mocks:
            for mock in mocks.values():
                mock.return_value = []
            await stats.dashboard(days=0, granularity="day")
            # 全量口径下趋势仍需要一个时间轴, 退化为 7 天而不是崩溃或画 400 天
            mocks["stats_trend"].assert_called_once_with(7, "day")
            mocks["stats_classes"].assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
