"""Storage regression checks using disposable SQLite databases and files only."""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from app.core import cleaner, storage
from app.api import system as system_api
from app.db import dao, database
from app.utils import file_utils
from app.utils.exceptions import AppError
from config import BASE_DIR, settings


class StorageSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="underwater_storage_test_")
        self.root = Path(self.temp.name)
        self.original_settings = {
            name: getattr(settings, name)
            for name in ("data_dir", "upload_dir", "result_dir", "db_path", "file_retention_days")
        }
        self.original_connection = getattr(database._local, "conn", None)
        database._local.conn = None
        settings.data_dir = self.root
        settings.upload_dir = self.root / "uploads"
        settings.result_dir = self.root / "results"
        settings.db_path = self.root / "test.sqlite"
        settings.file_retention_days = 7
        settings.upload_dir.mkdir()
        settings.result_dir.mkdir()
        # Never leave an exit hook that could later open the user's real DB.
        with mock.patch.object(database, "_register_exit_hook"):
            database.init_db()

    def tearDown(self) -> None:
        database.close_connection()
        database._local.conn = self.original_connection
        for name, value in self.original_settings.items():
            setattr(settings, name, value)
        self.temp.cleanup()

    def make_file(self, name: str, content: bytes = b"test file", *, directory: str = "uploads",
                  age_days: int = 20) -> Path:
        base = settings.upload_dir if directory == "uploads" else settings.result_dir
        path = base / name
        path.write_bytes(content)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def record(self, path: Path | None = None, *, age_days: int = 20,
               result_video: Path | None = None) -> int:
        return dao.insert_detection({
            "task_type": "video" if result_video else "image",
            "source_name": path.name if path else "test.jpg",
            "saved_name": path.name if path else None,
            "result_video": result_video.name if result_video else None,
            "created_at": (dt.datetime.now() - dt.timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S"),
        })

    def snapshot(self) -> dict[str, object]:
        # SQLite read locks update the transient WAL shared-memory index even
        # for SELECTs; persisted database/WAL, uploads, trash and log stay fixed.
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file() and not p.name.endswith("-shm")}

    def test_expired_dry_run_is_read_only_and_counts_shared_files_once(self) -> None:
        expired = self.make_file("expired.jpg")
        shared = self.make_file("shared.jpg")
        orphan = self.make_file("orphan.jpg")
        ids = [self.record(expired), self.record(expired), self.record(shared), self.record(shared, age_days=0)]
        before = self.snapshot()

        result = cleaner.cleanup_expired(7, dry_run=True)

        self.assertEqual(self.snapshot(), before)
        self.assertFalse(storage.trash_dir(create=False).exists())
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["freed_bytes"], expired.stat().st_size + orphan.stat().st_size)
        self.assertTrue(all(dao.get_record(rid) for rid in ids))

    def test_system_endpoint_dry_run_is_read_only_without_double_counting(self) -> None:
        source = self.make_file("source.jpg")
        self.record(source)
        self.make_file("orphan_a.jpg", b"duplicate")
        self.make_file("orphan_b.jpg", b"duplicate")
        before = self.snapshot()

        async def same_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        manager_module = types.ModuleType("app.core.video_task")
        manager_module.task_manager = []
        with mock.patch.dict(sys.modules, {"app.core.video_task": manager_module}), \
                mock.patch.object(system_api, "run_in_threadpool", same_thread):
            response = asyncio.run(system_api.full_cleanup(days=7, duplicates=True, dry_run=True))

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(response["data"]["freed_bytes"], len(b"test file") + len(b"duplicate") * 2)
        self.assertEqual(response["data"]["duplicates"]["count"], 0)
        self.assertTrue(response["data"]["dry_run"])

    def test_cleanup_endpoint_has_one_canonical_registration(self) -> None:
        routes = [route for route in system_api.router.routes if route.path == "/api/system/cleanup"]
        self.assertEqual(len(routes), 1)
        parsed = ast.parse((BASE_DIR / "app/main.py").read_text(encoding="utf-8"))
        for node in ast.walk(parsed):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and decorator.args:
                        self.assertNotEqual(getattr(decorator.args[0], "value", None), "/api/system/cleanup")

    def test_trash_api_lists_entry_time_and_hides_metadata_files(self) -> None:
        source = self.make_file("video_old.mp4", directory="results", age_days=400)
        storage._move_to_trash(source)

        async def same_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with mock.patch.object(system_api, "run_in_threadpool", same_thread):
            response = asyncio.run(system_api.trash_list(limit=50))
        self.assertEqual(response["data"]["files"], 1)
        entries = response["data"]["files_detail"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["original_name"], source.name)
        self.assertEqual(entries[0]["directory"], "results")
        self.assertEqual(entries[0]["age_days"], 0)

    def test_real_expiry_keeps_current_shared_references(self) -> None:
        shared = self.make_file("shared.jpg")
        expired_id = self.record(shared)
        retained_id = self.record(shared, age_days=0)
        result = cleaner.cleanup_expired(7)
        self.assertEqual(result["records"], 1)
        self.assertIsNone(dao.get_record(expired_id))
        self.assertIsNotNone(dao.get_record(retained_id))
        self.assertTrue(shared.exists())
        self.assertEqual(result["freed_bytes"], 0)

    def test_retention_disabled_keeps_records_but_can_reclaim_orphans(self) -> None:
        source = self.make_file("source.jpg")
        orphan = self.make_file("orphan.jpg")
        rid = self.record(source)
        result = cleaner.cleanup_expired(0)
        self.assertIsNotNone(dao.get_record(rid))
        self.assertTrue(source.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(result["records"], 0)

    def test_old_file_gets_full_trash_window_from_actual_entry_time(self) -> None:
        source = self.make_file("very_old.jpg", age_days=400)
        entry = storage._move_to_trash(source)
        metadata = json.loads(storage._metadata_path(entry["trash"]).read_text())
        self.assertAlmostEqual(metadata["entered_at"], time.time(), delta=3)
        self.assertEqual(metadata["directory"], "uploads")
        self.assertEqual(storage.trash_stats()["files"], 1)
        self.assertEqual(storage.trash_stats()["oldest_days"], 0)
        self.assertEqual(storage.purge_trash(7)["count"], 0)
        self.assertTrue((storage.trash_dir() / entry["trash"]).exists())

    def test_expired_metadata_entry_is_purged_with_sidecar(self) -> None:
        entry = storage._move_to_trash(self.make_file("old.jpg"))
        metadata_path = storage._metadata_path(entry["trash"])
        metadata = json.loads(metadata_path.read_text())
        metadata["entered_at"] = time.time() - 8 * 86400
        metadata_path.write_text(json.dumps(metadata))
        self.assertEqual(storage.purge_trash(7)["count"], 1)
        self.assertFalse(metadata_path.exists())
        self.assertEqual(storage.trash_stats()["files"], 0)

    def test_legacy_timestamp_retention_ignores_original_mtime(self) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        fresh = storage.trash_dir() / f"{stamp}_123_old.jpg"
        fresh.write_bytes(b"fresh trash")
        original_time = time.time() - 400 * 86400
        os.utime(fresh, (original_time, original_time))
        past_stamp = (dt.datetime.now() - dt.timedelta(days=8)).strftime("%Y%m%d_%H%M%S")
        old = storage.trash_dir() / f"{past_stamp}_456_recent.jpg"
        old.write_bytes(b"old trash")
        self.assertEqual(storage.purge_trash(7)["count"], 1)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())

    def test_unknown_legacy_time_is_only_removed_on_explicit_empty(self) -> None:
        unknown = storage.trash_dir() / "unrecognized.jpg"
        unknown.write_bytes(b"preserve")
        self.assertEqual(storage.purge_trash(7)["count"], 0)
        self.assertEqual(storage.purge_trash(0)["count"], 1)

    def test_video_restore_uses_original_directory(self) -> None:
        source = self.make_file("video_test.mp4", directory="results")
        entry = storage._move_to_trash(source)
        result = storage.restore_from_trash(entry["trash"])
        self.assertEqual(Path(result["path"]), source)
        self.assertEqual(source.read_bytes(), b"test file")
        self.assertFalse((settings.upload_dir / source.name).exists())
        self.assertFalse(storage._metadata_path(entry["trash"]).exists())

    def test_metadata_restores_arbitrary_result_filename_without_prefix(self) -> None:
        source = self.make_file("custom.mp4", directory="results")
        entry = storage._move_to_trash(source)
        result = storage.restore_from_trash(entry["trash"])
        self.assertEqual(Path(result["path"]), source)

    def test_legacy_video_restore_uses_results_fallback(self) -> None:
        legacy = storage.trash_dir() / "20260101_120000_123_video_legacy.mp4"
        legacy.write_bytes(b"legacy result")
        result = storage.restore_from_trash(legacy.name)
        self.assertEqual(Path(result["path"]), settings.result_dir / "video_legacy.mp4")

    def test_restore_conflict_keeps_both_files_and_returns_409(self) -> None:
        source = self.make_file("conflict.jpg", b"old")
        entry = storage._move_to_trash(source)
        source.write_bytes(b"new")
        with self.assertRaises(AppError) as error:
            storage.restore_from_trash(entry["trash"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.code, "RESTORE_CONFLICT")
        self.assertEqual(source.read_bytes(), b"new")
        self.assertEqual((storage.trash_dir() / entry["trash"]).read_bytes(), b"old")
        self.assertTrue(storage._metadata_path(entry["trash"]).exists())

    def test_metadata_failure_does_not_move_original(self) -> None:
        source = self.make_file("stay.jpg")
        real_open = Path.open

        def fail_metadata(path, *args, **kwargs):
            if path.parent.name == ".metadata":
                raise OSError("simulated disk failure")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", fail_metadata):
            self.assertIsNone(storage._move_to_trash(source))
        self.assertTrue(source.exists())

    def test_stale_orphan_scan_cannot_remove_newly_referenced_file(self) -> None:
        source = self.make_file("racing.jpg")

        def stale_scan(**kwargs):
            self.record(source, age_days=0)
            return [(source, source.stat().st_size)]

        with mock.patch.object(storage, "scan_orphans", stale_scan):
            self.assertEqual(storage.cleanup_orphans(min_age_sec=0)["count"], 0)
            self.assertEqual(storage.cleanup_orphans(min_age_sec=0, to_trash=False)["count"], 0)
        self.assertTrue(source.exists())

    def test_active_upload_lease_blocks_reclamation(self) -> None:
        source = self.make_file("active.jpg")
        file_utils._register_upload(source)
        try:
            self.assertIsNone(storage._move_to_trash(source))
            self.assertEqual(storage.cleanup_orphans(min_age_sec=0)["count"], 0)
            self.assertEqual(cleaner._unlink(source), 0)
        finally:
            file_utils.release_upload(source)
        self.assertTrue(source.exists())

    def test_duplicate_cleanup_prefers_referenced_file_even_if_sorted_last(self) -> None:
        orphan = self.make_file("a_orphan.jpg", b"same")
        referenced = self.make_file("z_source.jpg", b"same")
        self.record(referenced, age_days=0)
        before = self.snapshot()
        self.assertEqual(cleaner.cleanup_duplicates(dry_run=True)["count"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(cleaner.cleanup_duplicates()["count"], 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(referenced.exists())

    def test_direct_unlink_rechecks_current_database_references(self) -> None:
        source = self.make_file("referenced.jpg")
        self.record(source, age_days=0)
        self.assertEqual(cleaner._unlink(source), 0)
        self.assertTrue(source.exists())

    def test_unmanaged_file_is_never_reclaimed(self) -> None:
        source = self.root / "outside_managed.jpg"
        source.write_bytes(b"keep")
        self.assertIsNone(storage._move_to_trash(source))
        self.assertEqual(cleaner._unlink(source), 0)
        self.assertTrue(source.exists())

    def test_read_only_trash_stats_does_not_create_directory(self) -> None:
        self.assertEqual(storage.trash_stats()["files"], 0)
        self.assertFalse(storage.trash_dir(create=False).exists())


if __name__ == "__main__":
    unittest.main()
