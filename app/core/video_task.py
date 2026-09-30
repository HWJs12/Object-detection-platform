"""视频检测任务: 逐帧推理 + 异步回调实时推送。

设计要点:
    - 阻塞的解码/推理/绘制全部放入独立线程, 避免阻塞 asyncio 事件循环
    - 支持跳帧(frame_skip)与长边缩放(video_max_side), 保证 CPU 环境下的流畅度
    - 预留目标跟踪扩展位: 后续可在 _process_frame 中接入 tracker, 替换逐帧统计逻辑
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.core.yolo_engine import engine
from app.db import dao
from app.utils.exceptions import AppError, InvalidFileError
from app.utils.image_utils import (
    count_by_class,
    draw_detections,
    resize_long_side,
    to_base64,
)
from config import settings


@dataclass
class VideoMeta:
    """视频元信息。"""

    fps: float = 0.0
    total_frames: int = 0
    width: int = 0
    height: int = 0
    duration_sec: float = 0.0


@dataclass
class VideoTaskResult:
    """视频任务最终统计。"""

    record_id: Optional[int] = None
    frames_processed: int = 0
    total_frames: int = 0
    duration_sec: float = 0.0
    avg_fps: float = 0.0
    peak_objects: int = 0
    avg_objects: float = 0.0
    total_detections: int = 0
    counts: List[Dict[str, Any]] = field(default_factory=list)


class VideoDetectionTask:
    """一次视频检测任务。"""

    MAX_SAMPLE_ITEMS = 200  # 入库明细采样上限, 避免数据库膨胀

    def __init__(self, task_id: str, video_path: Path, source_name: str,
                 conf_threshold: Optional[float] = None) -> None:
        self.task_id = task_id
        self.video_path = video_path
        self.source_name = source_name
        self.meta = VideoMeta()
        self.result = VideoTaskResult()
        self.cancelled = False
        self.paused = False
        # 本任务使用的置信度阈值(创建时指定, 运行中可通过控制接口实时调整)
        self.conf_threshold = float(
            settings.conf_threshold if conf_threshold is None else conf_threshold
        )
        # 运行状态(供控制接口查询)
        self.frame_index = 0
        self.processed = 0
        self._paused_seconds = 0.0
        self._pause_started = 0.0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-infer")

    # ---------------- 视频探测 ----------------
    def probe(self) -> VideoMeta:
        """读取视频基本信息, 校验可解码性。"""
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise InvalidFileError(
                "视频无法打开, 编码可能不受支持(建议转换为 MP4 / H.264 编码后重试)"
            )
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            ret, _ = cap.read()  # 试读一帧, 判断文件是否损坏
            if not ret:
                raise InvalidFileError("视频文件已损坏或无法解码")
            self.meta = VideoMeta(
                fps=fps,
                total_frames=total,
                width=width,
                height=height,
                duration_sec=round(total / fps, 2) if fps > 0 else 0.0,
            )
            return self.meta
        finally:
            cap.release()

    # ---------------- 单帧处理(在线程中执行) ----------------
    def _process_frame(self, frame: np.ndarray) -> Tuple[str, List[Dict[str, Any]], float]:
        """缩放 -> 推理 -> 绘制 -> 编码, 返回 (base64, 明细, 推理耗时ms)。"""
        small = resize_long_side(frame, settings.video_max_side)
        started = time.perf_counter()
        detections = engine.predict(small, conf=self.conf_threshold)
        infer_ms = (time.perf_counter() - started) * 1000
        drawn = draw_detections(small, detections)
        return to_base64(drawn, settings.jpeg_quality), [d.to_dict() for d in detections], infer_ms

    # ---------------- 主流程 ----------------
    async def run(self, send: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """逐帧检测并把结果通过 send 回调实时推送。"""
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise InvalidFileError("视频无法打开, 请确认文件未损坏且编码受支持")

        loop = asyncio.get_event_loop()
        frame_skip = max(1, int(settings.frame_skip))
        min_interval = 1.0 / settings.push_fps_limit if settings.push_fps_limit > 0 else 0.0

        frame_index = 0
        processed = 0
        sum_objects = 0
        peak_objects = 0
        total_infer_ms = 0.0
        class_counter: Dict[str, int] = {}
        class_id_of: Dict[str, int] = {}
        frame_rows: List[Tuple[int, float, int]] = []
        sample_items: List[Dict[str, Any]] = []
        started_at = time.perf_counter()
        last_push_at = 0.0
        last_sample_ts = -1.0

        try:
            while not self.cancelled:
                # 暂停状态: 不读帧、不推理、不推送, 保持当前播放位置等待恢复
                if self.paused:
                    if not self._pause_started:
                        self._pause_started = time.perf_counter()
                    await asyncio.sleep(0.1)
                    continue
                if self._pause_started:  # 刚从暂停恢复, 累计暂停时长(用于扣除)
                    self._paused_seconds += time.perf_counter() - self._pause_started
                    self._pause_started = 0.0

                ret, frame = await loop.run_in_executor(self._executor, cap.read)
                if not ret:
                    break

                timestamp = frame_index / self.meta.fps if self.meta.fps > 0 else 0.0
                do_detect = frame_index % frame_skip == 0

                if not do_detect:
                    frame_index += 1
                    self.frame_index = frame_index
                    continue

                image_b64, items, infer_ms = await loop.run_in_executor(
                    self._executor, self._process_frame, frame
                )

                # 推理期间可能收到暂停指令: 丢弃本帧结果, 不推送也不计数
                if self.paused:
                    continue

                processed += 1
                self.processed = processed
                total_infer_ms += infer_ms
                total_in_frame = len(items)
                sum_objects += total_in_frame
                peak_objects = max(peak_objects, total_in_frame)

                counts: Dict[str, int] = {}
                for it in items:
                    counts[it["class_name"]] = counts.get(it["class_name"], 0) + 1
                    class_counter[it["class_name"]] = class_counter.get(it["class_name"], 0) + 1
                    class_id_of.setdefault(it["class_name"], it["class_id"])
                counts_list = [
                    {"class_name": k, "count": v}
                    for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                ]

                # 采样保存逐帧统计与部分明细, 控制数据库体积
                if timestamp - last_sample_ts >= settings.frame_stats_interval:
                    last_sample_ts = timestamp
                    frame_rows.append((frame_index, round(timestamp, 2), total_in_frame))
                    if len(sample_items) < self.MAX_SAMPLE_ITEMS:
                        sample_items.extend(
                            {**it, "frame_index": frame_index} for it in items
                        )

                # 推流限速(检测本身较慢时不会触发)
                now = time.perf_counter()
                if now - last_push_at >= min_interval:
                    last_push_at = now
                    elapsed = now - started_at - self._paused_seconds
                    await send({
                        "type": "frame",
                        "frame_index": frame_index,
                        "timestamp": round(timestamp, 2),
                        "total": total_in_frame,
                        "counts": counts_list,
                        "detections": items,
                        "image": image_b64,
                        "fps": round(processed / elapsed, 2) if elapsed > 0 else 0.0,
                        "conf_threshold": self.conf_threshold,
                        "progress": (
                            round(frame_index / max(self.meta.total_frames, 1) * 100, 1)
                        ),
                    })

                frame_index += 1
                self.frame_index = frame_index
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            cap.release()

        if self.cancelled:
            return

        elapsed = time.perf_counter() - started_at - self._paused_seconds
        avg_fps = round(processed / elapsed, 2) if elapsed > 0 else 0.0
        avg_objects = round(sum_objects / processed, 2) if processed else 0.0

        # ---------------- 落库 ----------------
        created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record_id = dao.insert_detection({
            "task_type": "video",
            "source_name": self.source_name,
            "saved_name": self.video_path.name,
            "created_at": created_at,
            "total_objects": sum_objects,
            "image_width": self.meta.width,
            "image_height": self.meta.height,
            "inference_ms": round(total_infer_ms / processed, 1) if processed else 0.0,
            "frames_processed": processed,
            "avg_fps": avg_fps,
            "duration_sec": self.meta.duration_sec,
            "conf_threshold": self.conf_threshold,
            "status": "success",
            "message": f"单帧峰值 {peak_objects} 个, 平均每帧 {avg_objects} 个(未启用目标跟踪)",
        })
        dao.insert_items(record_id, sample_items)
        dao.insert_class_stats(
            record_id,
            [(class_id_of.get(name, -1), name, cnt) for name, cnt in
             sorted(class_counter.items(), key=lambda kv: (-kv[1], kv[0]))],
        )
        dao.insert_frame_stats(record_id, frame_rows)

        self.result = VideoTaskResult(
            record_id=record_id,
            frames_processed=processed,
            total_frames=self.meta.total_frames,
            duration_sec=self.meta.duration_sec,
            avg_fps=avg_fps,
            peak_objects=peak_objects,
            avg_objects=avg_objects,
            total_detections=sum_objects,
            counts=[
                {"class_name": k, "count": v}
                for k, v in sorted(class_counter.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        )

        await send({
            "type": "done",
            "record_id": record_id,
            "summary": {
                "frames_processed": processed,
                "conf_threshold": self.conf_threshold,
                "total_frames": self.meta.total_frames,
                "duration_sec": self.meta.duration_sec,
                "avg_fps": avg_fps,
                "peak_objects": peak_objects,
                "avg_objects": avg_objects,
                "total_detections": sum_objects,
                "counts": self.result.counts,
            },
        })

    # ---------------- 运行控制(暂停 / 继续 / 调整阈值) ----------------
    def pause(self) -> None:
        """暂停检测: 停止读帧与推流, 保持当前播放位置。"""
        self.paused = True
        self._pause_started = time.perf_counter()

    def resume(self) -> None:
        """继续检测: 从暂停位置接着处理。"""
        if self.paused and self._pause_started:
            self._paused_seconds += time.perf_counter() - self._pause_started
            self._pause_started = 0.0
        self.paused = False

    def set_conf(self, value: Any) -> float:
        """运行中调整置信度阈值(下一帧起生效)。"""
        try:
            conf = float(value)
        except (TypeError, ValueError):
            raise AppError("置信度阈值必须是数字", code="INVALID_CONF")
        if not 0.01 <= conf <= 0.99:
            raise AppError("置信度阈值需在 0.01 ~ 0.99 之间", code="INVALID_CONF")
        self.conf_threshold = conf
        return conf

    def status(self) -> Dict[str, Any]:
        """任务当前状态(供控制接口与前端展示)。"""
        return {
            "task_id": self.task_id,
            "paused": self.paused,
            "frame_index": self.frame_index,
            "processed": self.processed,
            "conf_threshold": self.conf_threshold,
            "total_frames": self.meta.total_frames,
        }

    def cancel(self) -> None:
        """请求取消任务。"""
        self.cancelled = True

    def shutdown(self) -> None:
        """释放线程池。"""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass


class VideoTaskManager:
    """视频任务注册表。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, VideoDetectionTask] = {}

    def create(self, video_path: Path, source_name: str,
               conf_threshold: Optional[float] = None) -> VideoDetectionTask:
        task_id = uuid.uuid4().hex[:12]
        task = VideoDetectionTask(task_id, video_path, source_name, conf_threshold)
        self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> Optional[VideoDetectionTask]:
        return self._tasks.get(task_id)

    def remove(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task:
            task.shutdown()

    def __len__(self) -> int:
        return len(self._tasks)


# 全局任务管理器
task_manager = VideoTaskManager()
