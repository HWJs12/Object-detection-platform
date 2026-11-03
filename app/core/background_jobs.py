"""Background video ownership and bounded retry deduplication for image requests."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from fastapi.concurrency import run_in_threadpool

from app.core.video_task import task_manager
from app.utils.exceptions import AppError

logger = logging.getLogger(__name__)


class VideoJob:
    def __init__(self, task):
        self.task = task
        self.runner = None
        self.condition = asyncio.Condition()
        self.sequence = 0
        self.last_event = None
        self.last_frame = None
        self.terminal = None
        self.finished_at = None
        self.started_at = time.perf_counter()

    async def publish(self, event):
        async with self.condition:
            self.sequence += 1
            self.last_event = event
            if event['type'] == 'frame':
                self.last_frame = event
            if event['type'] in ('done', 'error', 'stopped'):
                self.terminal = event
                self.finished_at = time.monotonic()
            self.condition.notify_all()

    def snapshot(self):
        task = self.task
        terminal = self.terminal
        state = ({'done': 'completed', 'error': 'failed', 'stopped': 'stopped'}[terminal['type']]
                 if terminal else 'stopping' if task.cancelled else 'paused' if task.paused
                 else 'running' if task.running else 'starting')
        return {
            **task.status(), 'source_name': task.source_name,
            'state': state, 'created_at': task.created_at,
            'meta': asdict(task.meta), 'config': {
                'conf_threshold': task.conf_threshold, 'device': task.parameters['device'],
                'frame_skip': task.parameters['frame_skip'],
            },
            'survey': dict(task.survey), 'terminal': terminal,
            'elapsed_active_sec': max(0, time.perf_counter() - self.started_at
                - task._paused_seconds - (time.perf_counter() - task._pause_started if task.paused and task._pause_started else 0)),
            'progress': min(100.0, task.frame_index / task.meta.total_frames * 100)
                        if task.meta.total_frames else 0,
        }

    async def next_event(self, after):
        async with self.condition:
            await self.condition.wait_for(lambda: self.sequence > after)
            return self.sequence, self.last_event


class VideoJobHub:
    HISTORY_LIMIT = 20
    HISTORY_TTL = 3600

    def __init__(self):
        self.jobs = {}

    def prune(self):
        now = time.monotonic()
        finished = sorted((job for job in self.jobs.values()
                           if job.runner and job.runner.done()),
                          key=lambda job: job.finished_at or now, reverse=True)
        for index, job in enumerate(finished):
            if index >= self.HISTORY_LIMIT or now - (job.finished_at or now) > self.HISTORY_TTL:
                self.jobs.pop(job.task.task_id, None)

    def start(self, task_id):
        self.prune()
        if task_id in self.jobs:
            return self.jobs[task_id]
        task = task_manager.claim(task_id)
        if task is None:
            raise AppError('视频任务不存在或服务已重启，请重新上传', code='TASK_NOT_FOUND', status_code=404)
        job = VideoJob(task)
        self.jobs[task_id] = job
        job.runner = asyncio.create_task(self._run(job))
        return job

    async def _run(self, job):
        task = job.task
        terminal = None

        async def publish(event):
            nonlocal terminal
            if event['type'] in ('done', 'error', 'stopped'):
                terminal = event
            else:
                await job.publish(event)

        try:
            await task.run(publish)
            terminal = terminal or {'type': 'stopped', 'message': '已停止检测，未完成的结果不保存'}
        except asyncio.CancelledError:
            task.cancel()
            terminal = {'type': 'stopped', 'message': '服务正在关闭，任务已停止'}
        except AppError as exc:
            task.cancel()
            terminal = {'type': 'error', 'message': exc.message, 'code': exc.code}
        except Exception:
            task.cancel()
            logger.exception('后台视频检测失败: %s', task.task_id)
            terminal = {'type': 'error', 'message': '视频检测失败，请查看服务日志后重试'}
        finally:
            try:
                await run_in_threadpool(task_manager.remove, task.task_id)
            except Exception:
                logger.exception('视频任务资源释放失败: %s', task.task_id)
            # Completion becomes visible only after workers and the tracking slot are released.
            await job.publish(terminal or {'type': 'stopped', 'message': '任务已停止'})
            self.prune()

    def get(self, task_id):
        self.prune()
        return self.jobs.get(task_id)

    def snapshots(self):
        self.prune()
        return [job.snapshot() for job in sorted(self.jobs.values(),
                key=lambda job: job.task.created_at, reverse=True)]

    async def shutdown(self):
        runners = []
        for job in self.jobs.values():
            if job.runner and not job.runner.done():
                job.task.cancel()
                runners.append(job.runner)
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)


class ImageRequestCache:
    """The same retry key joins one operation, including after a lost HTTP response."""
    LIMIT = 200
    TTL = 3600

    def __init__(self):
        self.entries = {}

    def claim(self, key: str, signature: str, operation: Callable[[], Awaitable[Any]]):
        now = time.monotonic()
        for saved_key, (_, future, created) in list(self.entries.items()):
            if future.done() and now - created > self.TTL:
                self.entries.pop(saved_key, None)
        existing = self.entries.get(key)
        if existing:
            previous, future, _ = existing
            if previous != signature:
                raise AppError('重试标识与图片或参数不一致，请重新选择图片', code='REQUEST_CONFLICT', status_code=409)
            if not future.done() or (not future.cancelled() and future.exception() is None):
                return future, False
            self.entries.pop(key, None)
        if len(self.entries) >= self.LIMIT:
            finished = [k for k, (_, future, _) in self.entries.items() if future.done()]
            if not finished:
                raise AppError('图片任务繁忙，请稍后重试', code='IMAGE_BUSY', status_code=429)
            self.entries.pop(finished[0])
        future = asyncio.create_task(operation())
        # Retrieve exceptions even if the browser leaves before the result arrives.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self.entries[key] = (signature, future, now)
        return future, True

    async def shutdown(self):
        pending = [future for _, future, _ in self.entries.values() if not future.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


video_jobs = VideoJobHub()
image_requests = ImageRequestCache()
