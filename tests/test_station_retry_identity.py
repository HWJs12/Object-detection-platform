"""Regression checks for stable station IDs in retries and historical statistics."""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.api import detect
from app.core import storage
from app.core.background_jobs import ImageRequestCache
from app.db import dao, database, stations
from app.main import app
from config import settings


class StationIdentityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='station_identity_')
        self.root = Path(self.temp.name)
        self.old_connection = getattr(database._local, 'conn', None)
        database._local.conn = None
        for name, value in {'db_path': self.root/'test.db', 'upload_dir': self.root/'uploads',
                            'result_dir': self.root/'results'}.items():
            self.enterContext(patch.object(settings, name, value))
        settings.upload_dir.mkdir()
        settings.result_dir.mkdir()
        with patch.object(database, '_register_exit_hook'):
            database.init_db()
        self.connection = database.get_connection()
        for module in (database, dao, stations, storage):
            self.enterContext(patch.object(module, 'get_connection', return_value=self.connection))
        self.enterContext(patch.object(detect, 'image_requests', ImageRequestCache()))
        self.client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 11111))
        self.image = cv2.imencode('.png', np.zeros((8,8,3), dtype=np.uint8))[1].tobytes()

    def tearDown(self):
        self.client.close()
        self.connection.close()
        database._local.conn = self.old_connection
        self.temp.cleanup()

    def create(self, name='临时调查站'):
        return stations.save_station({'name': name, 'x': .2, 'y': .3})

    def rename(self, station, name):
        return stations.save_station({**station, 'name': name}, station['id'])

    def save_record(self, station=None, total=3, inference_ms=None):
        record = {'task_type': 'image', 'source_name': 'test.png',
                  'created_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  'total_objects': total, 'inference_ms': inference_ms}
        if station:
            record.update(site_id=station['id'], site=station['name'])
        return dao.save_detection(record, [], [])

    def upload(self, station, key):
        return self.client.post('/api/detect/image',
            data={'site_id': station['id'], 'request_key': key, 'conf': '.25'},
            files={'file': ('same.png', self.image, 'image/png')})

    def fake_detection(self, path, name, conf, saved_name, survey):
        rid = dao.save_detection({'task_type': 'image', 'source_name': name,
            'saved_name': saved_name, 'total_objects': 0,
            'created_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), **survey}, [], [])
        return {'record_id': rid, 'total': 0, 'counts': [], 'detections': []}

    def test_same_request_recovers_after_station_rename(self):
        station = self.create()
        with patch.object(detect, '_read_and_detect', side_effect=self.fake_detection) as infer:
            first = self.upload(station, 'station-retry-identity-20261028')
            self.assertEqual(first.status_code, 200, first.text)
            renamed = self.rename(station, '更新后的调查站')
            recovered = self.upload(renamed, 'station-retry-identity-20261028')
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(first.json()['data']['record_id'], recovered.json()['data']['record_id'])
            self.assertEqual(infer.call_count, 1)
        self.assertEqual(dao.list_records()[1], 1)

    def test_different_station_still_rejects_reused_request_key(self):
        first_station, second_station = self.create('站点一'), self.create('站点二')
        with patch.object(detect, '_read_and_detect', side_effect=self.fake_detection):
            self.assertEqual(self.upload(first_station, 'station-conflict-key-20261028').status_code, 200)
            response = self.upload(second_station, 'station-conflict-key-20261028')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'REQUEST_CONFLICT')
        self.assertEqual(dao.list_records()[1], 1)

    def test_deleted_name_reuse_keeps_new_and_historical_groups_separate(self):
        old = self.create('复用的站名')
        old_record = self.save_record(old, 3)
        stations.delete_station(old['id'], old['version'])
        replacement = self.create('复用的站名')
        self.save_record(replacement, 5)
        self.assertIsNone(dao.get_record(old_record)['site_id'])
        self.assertNotEqual(old['id'], replacement['id'])
        grouped = dao.stats_sites(deduplicate=False)
        self.assertEqual(len(grouped), 2)
        current = next(row for row in grouped if row['site_id'] == replacement['id'])
        history = next(row for row in grouped if row['site_id'] is None)
        self.assertEqual((current['records'], current['objects'], current['legacy_site']), (1, 5, False))
        self.assertEqual((history['records'], history['objects'], history['legacy_site']), (1, 3, True))
        self.assertEqual(current['site'], history['site'])
        self.assertEqual(sum(row['objects'] for row in grouped), 8)
        self.assertEqual(len(dao.stats_today()['by_site']), 2)

    def test_rename_preserves_one_identity_and_unlabeled_rows(self):
        station = self.create()
        self.save_record(station, 3)
        renamed = self.rename(station, '改名后的站点')
        self.save_record(renamed, 5)
        self.save_record(None, 2)
        grouped = dao.stats_sites(deduplicate=False)
        current = next(row for row in grouped if row['site_id'] == station['id'])
        self.assertEqual((current['records'], current['objects'], current['site_label']), (2, 8, '改名后的站点'))
        unlabeled = next(row for row in grouped if row['site_id'] is None)
        self.assertEqual((unlabeled['site_label'], unlabeled['objects']), ('未标注站点', 2))

    def test_recent_returns_measured_inference_time_and_missing_values(self):
        self.save_record(inference_ms=12.3)
        self.save_record(inference_ms=None)
        recent = dao.stats_recent(2)
        self.assertIsNone(recent[0]['inference_ms'])
        self.assertEqual(recent[1]['inference_ms'], 12.3)
