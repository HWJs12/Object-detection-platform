"""Regression checks for bugs observed in the real browser acceptance run."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from app.api import records, stats


class AttachmentHeaderTests(unittest.TestCase):
    def test_chinese_video_name_is_downloadable(self):
        name = 'repro_17_屏幕录制 2026-10-09 173746.zip'
        response = records._attachment_response(b'zip bytes', name, 'application/zip')
        header = response.headers['content-disposition']
        header.encode('ascii')
        self.assertEqual(unquote(header.split("filename*=UTF-8''", 1)[1]), name)
        self.assertEqual(response.body, b'zip bytes')

    def test_unsafe_filename_cannot_break_download_header(self):
        name = '测试"\\\r\n.zip'
        response = records._attachment_response(b'x', name, 'application/zip')
        header = response.headers['content-disposition']
        self.assertNotIn('\r', header)
        self.assertNotIn('\n', header)
        self.assertEqual(unquote(header.split("filename*=UTF-8''", 1)[1]), name)


class DashboardWindowTests(unittest.IsolatedAsyncioTestCase):
    async def check_window(self, buckets, granularity, expected_days):
        class FrozenDate(dt.date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 24)  # Saturday; the current week has six days.
        names = ['stats_overview', 'stats_trend', 'stats_class_trend', 'stats_hourly',
                 'stats_classes', 'stats_type_distribution', 'stats_recent',
                 'stats_today', 'stats_sites', 'stats_confidence']
        mocks = {name: self.enterContext(patch.object(stats.dao, name, return_value=[]))
                 for name in names}
        with patch.object(stats.dt, 'date', FrozenDate):
            await stats.dashboard(days=buckets, granularity=granularity)
        mocks['stats_classes'].assert_called_once_with(expected_days)
        mocks['stats_sites'].assert_called_once_with(expected_days, '', True)
        mocks['stats_confidence'].assert_called_once_with(expected_days, 10, '')
        mocks['stats_trend'].assert_called_once_with(buckets or 7, granularity)

    async def test_eight_weeks_include_all_fifty_five_calendar_days(self):
        await self.check_window(8, 'week', 55)

    async def test_day_filter_keeps_its_selected_window(self):
        await self.check_window(7, 'day', 7)

    async def test_all_history_keeps_unbounded_class_selection(self):
        await self.check_window(0, 'week', 0)
