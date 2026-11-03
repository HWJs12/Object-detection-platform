"""Lifecycle checks for server-owned video jobs and safe image retries."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core import background_jobs
from app.utils.exceptions import AppError


class ImageRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_retries_share_one_result(self):
        cache = background_jobs.ImageRequestCache()
        finish = asyncio.Event()
        calls = []
        async def detect():
            calls.append(1)
            await finish.wait()
            return {'record_id': 42}
        first, owner = cache.claim('key', 'same-file', detect)
        second, duplicate = cache.claim('key', 'same-file', detect)
        self.assertTrue(owner)
        self.assertFalse(duplicate)
        self.assertIs(first, second)
        finish.set()
        self.assertEqual(await first, {'record_id': 42})
        recovered, _ = cache.claim('key', 'same-file', detect)
        self.assertEqual(await recovered, {'record_id': 42})
        self.assertEqual(len(calls), 1)

    async def test_changed_file_or_parameters_reject_reused_key(self):
        cache = background_jobs.ImageRequestCache()
        async def detect(): return 1
        await cache.claim('key', 'first-file', detect)[0]
        with self.assertRaises(AppError) as exc:
            cache.claim('key', 'different-file', detect)
        self.assertEqual(exc.exception.code, 'REQUEST_CONFLICT')

    async def test_failed_operation_can_retry_without_poisoning_key(self):
        cache = background_jobs.ImageRequestCache()
        async def fail(): raise AppError('temporary failure')
        with self.assertRaises(AppError):
            await cache.claim('key', 'same', fail)[0]
        async def success(): return 2
        self.assertEqual(await cache.claim('key', 'same', success)[0], 2)

    async def test_aborted_viewer_does_not_abort_inflight_detection(self):
        cache = background_jobs.ImageRequestCache()
        finish = asyncio.Event()
        async def detect():
            await finish.wait()
            return 3
        future, _ = cache.claim('key', 'same', detect)
        async def viewer(): return await asyncio.shield(future)
        waiting = asyncio.create_task(viewer())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.assertFalse(future.cancelled())
        finish.set()
        self.assertEqual(await future, 3)


@dataclass
class Meta:
    fps: float = 10
    total_frames: int = 10
    width: int = 64
    height: int = 64
    duration_sec: float = 1


class VideoBackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.finish = asyncio.Event()
        self.entered = asyncio.Event()
        self.calls = 0
        self.task = SimpleNamespace(task_id='video1', source_name='clip.mp4',
            created_at=1, running=False, cancelled=False, paused=False,
            parameters={'device':'cpu','frame_skip':1}, meta=Meta(),
            conf_threshold=0.25, survey={}, frame_index=0,
            _paused_seconds=0, _pause_started=0)
        self.task.status = lambda: {'task_id':'video1','frame_index':self.task.frame_index}
        self.task.cancel = lambda: (setattr(self.task, 'cancelled', True), self.finish.set())
        self.manager = SimpleNamespace(claim=Mock(return_value=self.task), remove=Mock())
        self.enterContext(patch.object(background_jobs, 'task_manager', self.manager))
        self.hub = background_jobs.VideoJobHub()

        async def run(send):
            self.calls += 1
            self.task.running = True
            self.entered.set()
            self.task.frame_index = 1
            await send({'type':'frame','frame_index':1,'image':'preview'})
            await self.finish.wait()
            self.task.running = False
            if not self.task.cancelled:
                await send({'type':'done','record_id':7,'summary':{}})
        self.task.run = run

    async def asyncTearDown(self):
        await self.hub.shutdown()

    async def test_multiple_viewers_join_one_execution(self):
        first = self.hub.start('video1')
        self.assertIs(first, self.hub.start('video1'))
        await asyncio.wait_for(self.entered.wait(), 2)
        self.assertEqual(self.calls, 1)
        self.manager.claim.assert_called_once()
        self.finish.set()
        await first.runner
        self.assertEqual(first.terminal['record_id'], 7)

    async def test_job_finishes_without_any_viewer_and_replays_result(self):
        job = self.hub.start('video1')
        await asyncio.wait_for(self.entered.wait(), 2)
        self.assertEqual(job.last_frame['image'], 'preview')
        self.finish.set()
        await job.runner
        self.assertEqual(self.hub.get('video1').snapshot()['state'], 'completed')
        self.assertEqual(self.hub.get('video1').terminal['record_id'], 7)
        self.assertEqual(self.calls, 1)

    async def test_terminal_result_waits_for_resource_cleanup(self):
        cleanup = asyncio.Event()
        original_remove = self.manager.remove
        async def blocking_remove(fn, *args):
            await cleanup.wait()
            return fn(*args)
        with patch.object(background_jobs, 'run_in_threadpool', blocking_remove):
            job = self.hub.start('video1')
            await self.entered.wait()
            self.finish.set()
            await asyncio.sleep(0.01)
            self.assertIsNone(job.terminal)
            cleanup.set()
            await job.runner
        original_remove.assert_called_once_with('video1')
        self.assertEqual(job.terminal['type'], 'done')

    async def test_stop_cleans_up_and_does_not_create_completion(self):
        job = self.hub.start('video1')
        await self.entered.wait()
        self.task.cancel()
        await job.runner
        self.assertEqual(job.terminal['type'], 'stopped')
        self.manager.remove.assert_called_once_with('video1')

    async def test_startup_error_is_retained_for_reconnection(self):
        async def fail(send): raise AppError('busy', code='TRACK_BUSY')
        self.task.run = fail
        job = self.hub.start('video1')
        await job.runner
        self.assertEqual(job.terminal['code'], 'TRACK_BUSY')
        self.assertEqual(job.snapshot()['state'], 'failed')

    async def test_shutdown_drains_active_job(self):
        job = self.hub.start('video1')
        await self.entered.wait()
        await self.hub.shutdown()
        self.assertTrue(job.runner.done())
        self.assertEqual(job.terminal['type'], 'stopped')

    async def test_slow_viewer_keeps_only_latest_frame(self):
        job = self.hub.start('video1')
        await self.entered.wait()
        for n in range(100):
            await job.publish({'type':'frame','frame_index':n})
        sequence, event = await job.next_event(0)
        self.assertEqual(event['frame_index'], 99)
        self.assertEqual(job.last_frame['frame_index'], 99)
        self.assertGreater(sequence, 99)
