"""Regression tests for complete, traceable and non-duplicated analysis data."""
from __future__ import annotations

import datetime as dt
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.api.records import _assemble_report_items
from app.core import exporter, report
from app.core.provenance import inference_snapshot
from app.db import dao, database
from config import settings


class DataIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="underwater_data_test_")
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
            "task_type": "image", "source_name": "sample.jpg", "saved_name": "sample.jpg",
            "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_objects": 5, "unique_objects": 5, "total_detections": 5,
            "counting_mode": "image_objects", "unique_count_valid": True,
            "status": "success", "image_width": 100, "image_height": 100,
        }
        record.update(overrides)
        return record

    def test_whole_detection_rolls_back_when_statistics_fail(self):
        with mock.patch.object(dao, "insert_class_stats", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                dao.save_detection(self.record(), [{"class_id": 0, "class_name": "urchin", "confidence": 0.9, "bbox": [1,2,3,4]}], [(0,"urchin",1)])
        conn = database.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM detection_items").fetchone()[0], 0)

    def test_legacy_images_backfill_but_video_counts_remain_unknown(self):
        image_id = dao.insert_detection(self.record(counting_mode="legacy_unknown", unique_objects=0, total_detections=0, unique_count_valid=0))
        video_id = dao.insert_detection(self.record(task_type="video", counting_mode="legacy_unknown", unique_objects=15, unique_count_valid=0))
        database._migrate(database.get_connection())
        database.get_connection().commit()
        self.assertEqual(dao.get_record(image_id)["unique_objects"], 5)
        self.assertTrue(dao.get_record(image_id)["unique_count_valid"])
        # 改造后: 去重不成立时不再把 unique_objects 置为 None 让界面出现"不可用",
        # 而是保留原值并由 effective_count / effective_count_source 说明取用口径。
        video = dao.get_record(video_id)
        self.assertFalse(video["unique_count_valid"])
        self.assertIn("effective_count_source", video)
        self.assertGreaterEqual(video["effective_count"], 0)
        self.assertIn(video["effective_count_source"],
                      ("unique_tracks", "maxn", "total_detections"))

    def test_snapshot_roundtrip_preserves_model_and_used_parameters(self):
        snapshot = {"model": {"name": "old.pt", "sha256": "a"*64}, "device": "cpu", "parameters": {"imgsz": 640}}
        rid = dao.save_detection(self.record(inference_snapshot=snapshot), [], [])
        self.assertEqual(dao.get_record(rid)["inference_snapshot"], snapshot)
        item = _assemble_report_items([dao.get_record(rid)])[0]
        with mock.patch.object(settings, "model_path", "models/new.pt"):
            self.assertIn("old.pt", report._provenance_labels([item])["model"])
            self.assertNotIn("new.pt", report._provenance_labels([item])["model"])

    def test_loaded_model_snapshot_does_not_read_unrelated_disk_weights(self):
        with mock.patch("app.core.provenance.file_sha256", side_effect=OSError("unreadable replacement")):
            snapshot = inference_snapshot({"model":{"name":"actual.pt","sha256":"known"},"device":"cpu"})
        self.assertEqual(snapshot["model"]["name"], "actual.pt")

    def test_export_fetches_all_501_detections(self):
        items = [{"class_id": 0, "class_name": "urchin", "confidence": 0.9, "bbox": [1,2,3,4]} for _ in range(501)]
        rid = dao.save_detection(self.record(total_objects=501), items, [(0,"urchin",501)])
        assembled = _assemble_report_items([dao.get_record(rid)])[0]
        self.assertEqual(len(assembled["detections"]), 501)
        self.assertEqual(assembled["total"], 501)

    def test_source_dedup_latest_analysis_not_processing_sum(self):
        for index in range(3):
            dao.save_detection(self.record(source_hash="same", source_name=f"rename_{index}.jpg", saved_name=f"copy_{index}.jpg"), [], [(0,"urchin",5)])
        dao.save_detection(self.record(task_type="video", saved_name="video.mp4"), [], [(0,"urchin",99)])
        self.assertEqual(dao.stats_classes(deduplicate=True, task_type="image")[0]["count"], 5)
        self.assertEqual(dao.stats_classes()[0]["count"], 114)

    def test_legacy_filename_and_new_hash_share_identity(self):
        dao.save_detection(self.record(source_hash="", saved_name="shared.jpg"), [], [(0,"urchin",5)])
        dao.save_detection(self.record(source_hash="new-hash", saved_name="shared.jpg"), [], [(0,"urchin",7)])
        self.assertEqual(dao.stats_classes(deduplicate=True, task_type="image")[0]["count"], 7)

    def test_global_dedup_precedes_daily_buckets(self):
        yesterday = (dt.datetime.now()-dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        dao.save_detection(self.record(source_hash="same", created_at=yesterday), [], [(0,"urchin",5)])
        dao.save_detection(self.record(source_hash="same"), [], [(0,"urchin",6)])
        trend = dao.stats_class_trend(2, deduplicate=True, task_type="image")
        self.assertEqual(trend["series"][0]["data"], [None,6])

    def test_weekly_category_trend_matches_main_bucket_dates(self):
        dao.save_detection(self.record(), [], [(0,"urchin",5)])
        main = dao.stats_trend(8, "week")
        categories = dao.stats_class_trend(8, "week")
        self.assertEqual(categories["days"], [point["day"] for point in main])
        self.assertEqual(len(categories["days"]), 8)

    def test_yolo_filenames_do_not_collide(self):
        items = [{"source_name": name, "width":100, "height":100, "detections":[]} for name in ("a.jpg","a_2.jpg","a.png")]
        with zipfile.ZipFile(io.BytesIO(exporter.build_yolo(items))) as archive:
            names = archive.namelist()
        self.assertEqual(len(names),len(set(names)))
        self.assertIn("labels/a_3.txt", names)

    def test_requested_yolo_category_numbering_is_unchanged(self):
        items = [{"source_name":"sample.jpg","width":100,"height":100,"detections":[{"class_id":3,"class_name":"scallop","bbox":[1,2,3,4]}]}]
        with zipfile.ZipFile(io.BytesIO(exporter.build_yolo(items))) as archive:
            self.assertTrue(archive.read("labels/sample.txt").startswith(b"3 "))
            self.assertEqual(archive.read("classes.txt").decode("utf-8").splitlines(),
                             ["__unknown_class_0__", "__unknown_class_1__",
                              "__unknown_class_2__", "scallop"])

    def test_report_does_not_claim_confidence_is_accuracy_or_density(self):
        item = {"total":20,"width":100,"height":100,"counts":[{"class_name":"urchin","count":20}],"detections":[{"confidence":0.99,"bbox":[1,2,3,4]}]}
        conclusion = " ".join(report._analyse(item)+report._batch_conclusion([item]))
        self.assertNotIn("可作为统计依据", conclusion)
        self.assertNotIn("高密度场景", conclusion)
        self.assertIn("不代表准确率", conclusion)


if __name__ == "__main__":
    unittest.main()
