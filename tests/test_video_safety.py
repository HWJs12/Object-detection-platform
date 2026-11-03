"""Focused regression tests; inference, uploads and persistence are isolated."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import cv2
import numpy as np

from app.api import detect as detect_api
from app.api import model as model_api
from app.core import video_task
from app.core.video_encoder import H264VideoWriter, ffmpeg_executable
from app.core.yolo_engine import YoloEngine
from app.utils.exceptions import AppError, ModelLoadError
from app.utils.file_utils import active_upload_names
from config import settings


class FakeCapture:
    def __init__(self, count=3):
        self.remaining = count
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if not self.remaining:
            return False, None
        self.remaining -= 1
        return True, np.zeros((8, 8, 3), dtype=np.uint8)

    def release(self):
        self.released = True


def detection(track_id=None):
    item = {"class_id": 0, "class_name": "fish", "confidence": 0.9, "bbox": [1, 1, 3, 3]}
    if track_id is not None:
        item["track_id"] = track_id
    return item


class VideoSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patches = {
            "result_dir": self.root, "save_result_video": False,
            "track_enabled": True, "frame_skip": 1, "push_fps_limit": 0.0,
            "frame_stats_interval": 0.1, "device": "cpu", "video_max_side": 960,
            "imgsz": 640, "conf_threshold": 0.25, "iou_threshold": 0.45,
            "max_det": 300, "jpeg_quality": 70,
        }
        for name, value in patches.items():
            self.enterContext(patch.object(settings, name, value))
        self.capture = FakeCapture()
        self.engine = SimpleNamespace(
            begin_video=Mock(), end_video=Mock(), reset_tracker=Mock(),
            model_provenance=Mock(return_value={"name": "mock.pt", "sha256": "loaded-hash"}),
            track_with_status=Mock(return_value=([], True, None)),
            predict=Mock(return_value=[]),
        )
        self.enterContext(patch.object(video_task, "engine", self.engine))
        self.enterContext(patch.object(video_task.cv2, "VideoCapture", return_value=self.capture))
        self.persist = self.enterContext(patch.object(video_task.dao, "save_detection", return_value=42))
        self.enterContext(patch.object(video_task, "file_sha256", return_value="source-hash"))
        self.enterContext(patch.object(video_task, "inference_snapshot", side_effect=lambda p: {"parameters": dict(p)}))
        self.task = video_task.VideoDetectionTask("test", self.root / "source.mp4", "source.mp4")
        self.task.meta = video_task.VideoMeta(fps=1, total_frames=3, width=8, height=8, duration_sec=3)
        self.addCleanup(self.task.shutdown)

    async def run_task(self, outputs):
        self.task._process_frame = Mock(side_effect=outputs)
        events = []

        async def send(payload):
            events.append(payload)

        await self.task.run(send)
        return events

    async def test_fallback_does_not_claim_unique_count(self):
        events = await self.run_task([("", [detection()], 1.0, False, "tracking unavailable")] * 3)
        record = self.persist.call_args.args[0]
        self.assertEqual(record["unique_objects"], 0)
        self.assertEqual(record["unique_count_valid"], 0)
        self.assertEqual(record["counting_mode"], "frame_detections")
        self.assertEqual(record["total_detections"], 3)
        self.assertIsNone(events[-1]["summary"]["unique_objects"])
        self.assertEqual(events[-1]["summary"]["counts"], [{"class_name": "fish", "count": 1}])
        self.assertEqual(events[-1]["summary"]["maxn_total"], 1)
        self.assertEqual(record["total_objects"], 1)
        self.assertTrue(events[-1]["summary"]["warnings"])

    async def test_partial_tracking_invalidates_entire_unique_summary(self):
        events = await self.run_task([
            ("", [detection(1)], 1.0, True, None),
            ("", [detection()], 1.0, False, "tracking unavailable"),
            ("", [detection(1)], 1.0, True, None),
        ])
        self.assertEqual(self.task.result.counting_mode, "tracking_partial")
        self.assertFalse(self.task.result.unique_count_valid)
        self.assertIsNone(self.task.result.unique_objects)
        self.assertEqual(events[-1]["summary"]["counts"][0]["count"], 1)
        self.assertEqual(events[-1]["summary"]["total_detections"], 3)
        self.assertEqual(events[-1]["summary"]["maxn_total"], 1)

    async def test_valid_tracking_counts_one_trajectory(self):
        events = await self.run_task([("", [detection(1)], 1.0, True, None)] * 3)
        self.assertEqual(self.task.result.unique_objects, 1)
        self.assertTrue(self.task.result.unique_count_valid)
        self.assertEqual(events[-1]["summary"]["counts"][0]["count"], 1)
        self.assertEqual(self.persist.call_args.args[0]["source_hash"], "source-hash")

    async def test_pause_during_inference_does_not_drop_or_renumber_frame(self):
        calls = 0
        events = []

        def process(frame, conf):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.task.pause()
            return "", [detection(1)], 1.0, True, None

        async def send(payload):
            events.append(payload)
            if payload["type"] == "paused":
                self.task.resume()

        self.task._process_frame = process
        await self.task.run(send)
        frames = [event for event in events if event["type"] == "frame"]
        self.assertEqual([frame["frame_index"] for frame in frames], [0, 1, 2])
        self.assertEqual([frame["timestamp"] for frame in frames], [0, 1, 2])
        self.assertEqual(self.task.result.frames_processed, 3)
        self.assertEqual(self.task.frame_index, 3)

    def test_global_configuration_does_not_change_task_processing(self):
        with patch.object(settings, "imgsz", 320), patch.object(settings, "video_max_side", 320):
            with patch.object(video_task, "resize_long_side", side_effect=lambda frame, size: frame) as resize:
                with patch.object(video_task, "to_base64", return_value=""):
                    self.task._process_frame(np.zeros((8, 8, 3), dtype=np.uint8), 0.35)
        self.assertEqual(resize.call_args.args[1], 960)
        self.assertEqual(self.engine.track_with_status.call_args.kwargs["imgsz"], 640)
        self.assertEqual(self.engine.track_with_status.call_args.kwargs["conf"], 0.35)

    async def test_confidence_timeline_persisted_with_initial_parameters(self):
        self.task.frame_index = 1
        self.task.set_conf(0.6)
        self.task.set_conf(0.6)
        await self.run_task([("", [], 1.0, True, None)] * 3)
        record = self.persist.call_args.args[0]
        snapshot = json.loads(record["inference_snapshot"])
        self.assertEqual(record["conf_threshold"], 0.25)
        self.assertEqual(snapshot["confidence_changes"], [
            {"effective_from_frame": 1, "previous": 0.25, "value": 0.6}
        ])

    async def test_duplicate_websocket_does_not_cancel_owner(self):
        from app.core import background_jobs
        entered, finish = asyncio.Event(), asyncio.Event()
        executions = []

        async def run(send):
            executions.append(1)
            self.task.running = True
            entered.set()
            await finish.wait()
            self.task.running = False

        self.task.run = run
        self.task.cancel = Mock(wraps=self.task.cancel)
        remove = Mock()

        def claim(task_id):
            self.task.claim()
            return self.task

        manager = SimpleNamespace(claim=claim, remove=remove)
        hub = background_jobs.VideoJobHub()

        def socket():
            return SimpleNamespace(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock(),
                                   receive=AsyncMock(return_value={'type': 'websocket.disconnect'}))

        with patch.object(background_jobs, 'task_manager', manager), patch.object(detect_api, 'video_jobs', hub):
            await asyncio.wait_for(detect_api.ws_video_detect(socket(), 'test'), 2)
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(detect_api.ws_video_detect(socket(), 'test'), 2)
            self.task.cancel.assert_not_called()
            remove.assert_not_called()
            self.assertEqual(len(executions), 1)
            finish.set()
            await asyncio.wait_for(hub.get('test').runner, 2)
        remove.assert_called_once_with('test')

    def test_claimed_task_is_protected_from_ttl_and_capacity_eviction(self):
        self.task.created_at = 0
        manager = video_task.VideoTaskManager()
        manager._tasks[self.task.task_id] = self.task
        self.assertIs(manager.claim(self.task.task_id), self.task)
        self.assertEqual(manager.reap(), 0)
        with patch.object(settings, "max_active_video_tasks", 1):
            with self.assertRaises(AppError) as raised:
                manager.create(self.root / "another.mp4", "another.mp4")
            self.assertEqual(raised.exception.code, "VIDEO_CAPACITY")
        self.assertIs(manager.get(self.task.task_id), self.task)

    async def test_cancel_waits_for_native_worker_before_resource_release(self):
        import threading

        entered = threading.Event()
        finished = threading.Event()

        def process(frame, conf):
            entered.set()
            finished.wait(2)
            return "", [], 1.0, True, None

        self.task._process_frame = process
        running = asyncio.create_task(self.task.run(AsyncMock()))
        await asyncio.to_thread(entered.wait, 2)
        running.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(self.capture.released)
        self.engine.end_video.assert_not_called()
        finished.set()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(self.capture.released)
        self.engine.end_video.assert_called_once()

    async def test_export_file_lease_survives_until_atomic_commit(self):
        self.task._save_video = True
        self.task.parameters["save_result_video"] = True
        self.task.result_video_path.write_bytes(b"synthetic export, never decoded")
        with patch.object(video_task, "H264VideoWriter", return_value=Mock()):
            self.task._write_frame(np.zeros((8, 8, 3), dtype=np.uint8))
        self.assertIn(self.task.result_video_name, active_upload_names())

        def save(*args):
            self.assertIn(self.task.result_video_name, active_upload_names())
            return 42

        self.persist.side_effect = save
        await self.run_task([("", [], 1.0, True, None)] * 3)
        self.assertNotIn(self.task.result_video_name, active_upload_names())
        self.assertTrue(self.task.result_video_path.exists())

    async def test_encoder_failure_warns_and_never_offers_broken_video(self):
        self.task._save_video = True
        self.task.parameters["save_result_video"] = True
        with patch.object(video_task, "H264VideoWriter", side_effect=RuntimeError("encoder absent")):
            self.task._write_frame(np.zeros((8, 8, 3), dtype=np.uint8))
        events = await self.run_task([("", [], 1.0, True, None)] * 3)
        self.assertIsNone(events[-1]["result_video_url"])
        self.assertTrue(events[-1]["summary"]["warnings"])
        self.assertNotIn(self.task.result_video_name, active_upload_names())


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "weights.pt"
        self.path.write_bytes(b"synthetic weights; never loaded by torch")
        self.enterContext(patch.object(settings, "model_path", str(self.path)))
        self.enterContext(patch.object(settings, "track_enabled", True))
        self.enterContext(patch.object(settings, "device", "cpu"))
        self.engine = YoloEngine()
        self.engine._warmup = Mock()

    def fake_yolo(self, factory):
        module = types.ModuleType("ultralytics")
        module.YOLO = factory
        return patch.dict(sys.modules, {"ultralytics": module})

    def test_reload_replaces_both_models_and_metadata(self):
        old_image, old_video = object(), object()
        image = SimpleNamespace(names={0: "new"})
        video = SimpleNamespace(names={0: "new"})
        self.engine._model, self.engine._track_model = old_image, old_video
        with self.fake_yolo(Mock(side_effect=[image, video])):
            self.engine.reload()
        self.assertIs(self.engine._model, image)
        self.assertIs(self.engine._track_model, video)
        self.assertEqual(self.engine.model_provenance(True)["name"], "weights.pt")
        self.assertEqual(self.engine._model_metadata, self.engine._track_metadata)

    def test_reload_failure_keeps_both_previous_instances(self):
        image, video = object(), object()
        self.engine._model, self.engine._track_model = image, video
        with self.fake_yolo(Mock(side_effect=[SimpleNamespace(names={}), RuntimeError("bad weights")])):
            with self.assertRaises(ModelLoadError):
                self.engine.reload()
        self.assertIs(self.engine._model, image)
        self.assertIs(self.engine._track_model, video)
        self.assertFalse(self.engine._reloading)

    def test_reload_rejected_for_nontracking_video_too(self):
        self.engine.begin_video(tracking=False)
        try:
            with self.assertRaises(AppError) as raised:
                self.engine.reload()
            self.assertEqual(raised.exception.code, "MODEL_BUSY")
        finally:
            self.engine.end_video(tracking=False)

    def test_prediction_metadata_remains_with_weights_during_concurrent_reload(self):
        import threading

        inference_entered = threading.Event()
        reload_built = threading.Event()
        result = []
        failures = []

        def old_predict(**kwargs):
            inference_entered.set()
            if not reload_built.wait(2):
                raise RuntimeError("reload test did not begin")
            return []

        old = SimpleNamespace(predict=old_predict)
        self.engine._model = old
        self.engine._model_path = self.path
        self.engine._model_metadata = {"sha256": "old-loaded-hash"}
        replacement = SimpleNamespace(names={0: "new"})
        factory_calls = 0

        def factory(path):
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 2:
                reload_built.set()
            return replacement

        def infer():
            try:
                result.append(self.engine.predict_with_provenance(np.zeros((8, 8, 3), dtype=np.uint8)))
            except BaseException as exc:
                failures.append(exc)

        def reload():
            try:
                self.engine.reload()
            except BaseException as exc:
                failures.append(exc)

        with self.fake_yolo(factory):
            first = threading.Thread(target=infer)
            first.start()
            self.assertTrue(inference_entered.wait(2))
            second = threading.Thread(target=reload)
            second.start()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(failures)
        self.assertEqual(result[0][1]["sha256"], "old-loaded-hash")
        self.assertIs(self.engine._model, replacement)


class ConfigAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_invalid_value_does_not_commit_first_value(self):
        with patch.object(settings, "conf_threshold", 0.25):
            with self.assertRaises(AppError):
                await model_api.update_config({"conf_threshold": 0.7, "imgsz": 99999})
            self.assertEqual(settings.conf_threshold, 0.25)

    async def test_fractional_integer_and_boolean_values_rejected(self):
        for payload in ({"frame_skip": 2.5}, {"frame_skip": True}):
            with self.assertRaises(AppError):
                await model_api.update_config(payload)


class H264ExportTests(unittest.TestCase):
    def test_real_h264_export_has_decodable_even_dimensions(self):
        try:
            ffmpeg_executable()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.mp4"
            writer = H264VideoWriter(path, 0.5, (65, 49))
            for _ in range(3):
                writer.write(np.zeros((49, 65, 3), dtype=np.uint8))
            writer.release()
            cap = cv2.VideoCapture(str(path))
            try:
                self.assertTrue(cap.isOpened())
                self.assertAlmostEqual(cap.get(cv2.CAP_PROP_FPS), 0.5, places=2)
                self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 3)
                valid, frame = cap.read()
                self.assertTrue(valid)
                self.assertEqual(frame.shape[:2], (50, 66))
                fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                codec = "".join(chr((fourcc >> (8 * n)) & 255) for n in range(4))
                self.assertIn(codec.lower(), ("h264", "avc1"))
            finally:
                cap.release()


if __name__ == "__main__":
    unittest.main()
