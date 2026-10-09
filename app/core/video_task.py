"""视频检测任务: 逐帧推理 + 异步回调实时推送。

设计要点:
    - 阻塞的解码/推理/绘制全部放入独立线程, 避免阻塞 asyncio 事件循环
    - 支持跳帧(frame_skip)与长边缩放(video_max_side), 保证 CPU 环境下的流畅度
    - 预留目标跟踪扩展位: 后续可在 _process_frame 中接入 tracker, 替换逐帧统计逻辑
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
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
from app.utils.image_utils import draw_detections, resize_long_side, to_base64
from config import settings

logger = logging.getLogger(__name__)

# 线程池已关闭时 _submit 返回的哨兵值, 供 run() 判定并终止循环
_CANCELLED = object()


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
    total_detections: int = 0   # 检测框累计数(所有帧求和)
    unique_objects: int = 0     # 唯一目标数(目标跟踪跨帧去重)
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
        # 结果视频导出(带检测框)
        self._save_video = bool(settings.save_result_video)
        self.result_video_name = f"video_{task_id}.mp4"
        self.result_video_path = settings.result_dir / self.result_video_name
        self.video_finalized = False      # 是否已确认视频去向(落库 or 确认无视频)
        self._writer = None
        # 资源安全: 线程池关闭标志。shutdown() 与 run() 的提交动作分处两个线程,
        # 存在"检查 cancelled 通过 → 池子被关闭 → 提交"的竞态窗口, 故单独设标志
        # 供 _submit 判定, 关闭后一律拒绝提交而不抛异常。
        self._executor_closed = False
        # 资源治理: 记录创建时间, 供僵尸任务(上传后未连接 WebSocket)回收使用
        self.created_at: float = time.time()
        self.running: bool = False        # 是否正在执行, 运行中的任务不回收
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
        """缩放 -> 跟踪推理 -> 编码原帧, 返回 (base64, 明细, 推理耗时ms)。

        注意: 返回的是**未绘制检测框的原始帧**。检测框改由前端 Canvas 绘制,
        这样才能支持按类别实时筛选、显示 track_id, 并省去服务端绘制开销。
        """
        small = resize_long_side(frame, settings.video_max_side)
        started = time.perf_counter()
        if settings.track_enabled:
            detections = engine.track(small, conf=self.conf_threshold)
        else:
            detections = engine.predict(small, conf=self.conf_threshold)
        infer_ms = (time.perf_counter() - started) * 1000
        if self._save_video:
            # 结果视频: 在检测帧上绘制检测框后写入
            # 输出帧率按跳帧折算(fps/frame_skip), 保证成片时长与原视频一致
            self._write_frame(draw_detections(small.copy(), detections))
        return to_base64(small, settings.jpeg_quality), [d.to_dict() for d in detections], infer_ms

    # ---------------- 结果视频 ----------------
    def _write_frame(self, frame_bgr: np.ndarray) -> None:
        """写入一帧带检测框的画面(懒创建 VideoWriter)。"""
        try:
            if self._writer is None:
                height, width = frame_bgr.shape[:2]
                out_fps = max(1.0, self.meta.fps / max(1, int(settings.frame_skip)))
                writer = cv2.VideoWriter(
                    str(self.result_video_path),
                    cv2.VideoWriter_fourcc(*"mp4v"),
                    out_fps,
                    (width, height),
                )
                if not writer.isOpened():
                    self._save_video = False
                    logger.warning("结果视频编码器初始化失败, 本次不导出视频")
                    return
                self._writer = writer
            self._writer.write(frame_bgr)
        except Exception as exc:  # 导出失败不影响检测主流程
            self._save_video = False
            logger.warning("结果视频写入失败, 已停止导出: %s", exc)

    def _close_writer(self) -> None:
        """释放视频写入器(释放后文件才完整)。"""
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception:
                pass
            self._writer = None

    def _cleanup_video(self) -> None:
        """删除未被采纳的结果视频(任务取消 / 异常时)。"""
        try:
            self._close_writer()
            self.result_video_path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _submit(self, loop, fn, *args):
        """向线程池提交任务, 池已关闭时返回 CANCELLED 哨兵而不抛异常。

        资源安全: `shutdown()`(由任务终止路径在另一线程调用)与 `run()` 的提交动作
        之间存在竞态 —— run() 通过 `while not self.cancelled` 检查后, 池子可能恰好
        被关闭, 此时 `run_in_executor` 会抛
        `RuntimeError: cannot schedule new futures after shutdown`。
        该异常对用户无意义(任务本就在取消中), 故在此统一收敛:
        关闭后直接返回哨兵, 由调用方终止循环。
        """
        if self._executor_closed:
            return _CANCELLED
        try:
            return await loop.run_in_executor(self._executor, fn, *args)
        except RuntimeError as exc:
            if "cannot schedule new futures" in str(exc):
                return _CANCELLED
            raise

    # ---------------- 主流程 ----------------
    async def run(self, send: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """逐帧检测并把结果通过 send 回调实时推送。

        跟踪推理为全局独占资源: 进入时独占获取槽位, 退出/异常时释放。
        """
        use_track_slot = bool(settings.track_enabled)
        if use_track_slot:
            engine.acquire_track_slot()
        self.running = True

        try:
            cap = cv2.VideoCapture(str(self.video_path))
            if not cap.isOpened():
                raise InvalidFileError("视频无法打开, 请确认文件未损坏且编码受支持")

            loop = asyncio.get_running_loop()
            frame_skip = max(1, int(settings.frame_skip))
            min_interval = 1.0 / settings.push_fps_limit if settings.push_fps_limit > 0 else 0.0

            # 新任务开始: 清空跟踪器状态, 轨迹 ID 从 1 重新编号
            if settings.track_enabled:
                engine.reset_tracker()

            frame_index = 0
            processed = 0
            total_detections = 0        # 检测框累计数(所有帧求和)
            unique_objects = 0          # 唯一目标数(跟踪跨帧去重)
            tracked: Dict[int, Dict[str, Any]] = {}   # track_id -> 轨迹信息
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

                    _read_out = await self._submit(loop, cap.read)
                    if _read_out is _CANCELLED:
                        break
                    ret, frame = _read_out
                    if not ret:
                        break

                    timestamp = frame_index / self.meta.fps if self.meta.fps > 0 else 0.0
                    do_detect = frame_index % frame_skip == 0

                    if not do_detect:
                        frame_index += 1
                        self.frame_index = frame_index
                        continue

                    _frame_out = await self._submit(loop, self._process_frame, frame)
                    if _frame_out is _CANCELLED:
                        break
                    image_b64, items, infer_ms = _frame_out

                    # 推理期间可能收到暂停指令: 丢弃本帧结果, 不推送也不计数
                    if self.paused:
                        continue

                    processed += 1
                    self.processed = processed
                    total_infer_ms += infer_ms
                    total_in_frame = len(items)
                    total_detections += total_in_frame
                    peak_objects = max(peak_objects, total_in_frame)

                    counts: Dict[str, int] = {}
                    for it in items:
                        name = it["class_name"]
                        counts[name] = counts.get(name, 0) + 1
                        class_id_of.setdefault(name, it["class_id"])

                        tid = it.get("track_id")
                        if tid is None:
                            # 跟踪不可用: 退化为逐帧计数(与旧口径一致)
                            class_counter[name] = class_counter.get(name, 0) + 1
                            unique_objects += 1
                            continue
                        rec = tracked.get(tid)
                        if rec is None:
                            # 该 track_id 首次出现: 计入唯一目标与类别统计
                            tracked[tid] = {"class_name": name, "frames": 1,
                                            "max_conf": it["confidence"]}
                            class_counter[name] = class_counter.get(name, 0) + 1
                            unique_objects += 1
                        else:
                            rec["frames"] += 1
                            rec["max_conf"] = max(rec["max_conf"], it["confidence"])
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
                            "unique": unique_objects,
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
                self._close_writer()   # 释放后视频文件才完整

            if self.cancelled:
                self._cleanup_video()  # 任务被取消: 丢弃半成品视频
                return

            elapsed = time.perf_counter() - started_at - self._paused_seconds
            avg_fps = round(processed / elapsed, 2) if elapsed > 0 else 0.0
            avg_objects = round(total_detections / processed, 2) if processed else 0.0

            # 结果视频: 仅当文件确实生成且非空时入库
            result_video_name = None
            try:
                if (self._save_video and self.result_video_path.exists()
                        and self.result_video_path.stat().st_size > 0):
                    result_video_name = self.result_video_name
            except OSError:
                result_video_name = None

            # ---------------- 落库 ----------------
            created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record_id = dao.insert_detection({
                "task_type": "video",
                "source_name": self.source_name,
                "saved_name": self.video_path.name,
                "result_video": result_video_name,
                "created_at": created_at,
                "total_objects": unique_objects,
                "unique_objects": unique_objects,
                "total_detections": total_detections,
                "image_width": self.meta.width,
                "image_height": self.meta.height,
                "inference_ms": round(total_infer_ms / processed, 1) if processed else 0.0,
                "frames_processed": processed,
                "avg_fps": avg_fps,
                "duration_sec": self.meta.duration_sec,
                "conf_threshold": self.conf_threshold,
                "status": "success",
                "message": (
                    f"目标跟踪去重: 唯一目标 {unique_objects} 个 / 累计检测框 {total_detections} 个; "
                    f"单帧峰值 {peak_objects} 个, 平均每帧 {avg_objects} 个"
                ),
            })
            dao.insert_items(record_id, sample_items)
            dao.insert_class_stats(
                record_id,
                [(class_id_of.get(name, -1), name, cnt) for name, cnt in
                 sorted(class_counter.items(), key=lambda kv: (-kv[1], kv[0]))],
            )
            dao.insert_frame_stats(record_id, frame_rows)

            # 视频去向已确认: 记录已入库, 结果视频(若有)保留
            self.video_finalized = True

            self.result = VideoTaskResult(
                record_id=record_id,
                frames_processed=processed,
                total_frames=self.meta.total_frames,
                duration_sec=self.meta.duration_sec,
                avg_fps=avg_fps,
                peak_objects=peak_objects,
                avg_objects=avg_objects,
                total_detections=total_detections,
                unique_objects=unique_objects,
                counts=[
                    {"class_name": k, "count": v}
                    for k, v in sorted(class_counter.items(), key=lambda kv: (-kv[1], kv[0]))
                ],
            )

            await send({
                "type": "done",
                "record_id": record_id,
                "result_video_url": (
                    f"/data/results/{result_video_name}" if result_video_name else None
                ),
                "summary": {
                    "frames_processed": processed,
                    "conf_threshold": self.conf_threshold,
                    "total_frames": self.meta.total_frames,
                    "duration_sec": self.meta.duration_sec,
                    "avg_fps": avg_fps,
                    "peak_objects": peak_objects,
                    "avg_objects": avg_objects,
                    "total_detections": total_detections,
                    "unique_objects": unique_objects,
                    "counts": self.result.counts,
                },
            })
        finally:
            self.running = False
            if use_track_slot:
                engine.release_track_slot()

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

    def delete_source(self) -> None:
        """删除本任务的上传源文件(仅用于僵尸任务回收)。

        已完成任务的文件保留在磁盘, 由文件保留策略(FileCleaner)按天数统一处理,
        避免用户在浏览结果时源文件突然消失。
        """
        try:
            self.video_path.unlink(missing_ok=True)
        except OSError:
            pass

    def shutdown(self) -> None:
        """释放线程池; 若结果视频未被采纳(任务取消 / 异常), 删除半成品文件。

        资源安全: 必须等 executor 中的 worker 完全结束后再释放 writer / 清理视频,
        否则 `_close_writer()` / `_cleanup_video()` 会与仍在运行的 worker 竞跑,
        导致 `cannot schedule new futures after shutdown` 与 cv2 `Unknown C++ exception`。

        注意: 本方法会阻塞直至 worker 结束, 调用方若处于事件循环线程,
        必须通过 `run_in_threadpool` 调用(见 app/api/detect.py), 否则会卡死事件循环。
        cancel_futures 需 Python 3.9+, 本项目解释器为 3.13, 可直接使用。
        """
        # 先置标志: 令 run() 中任何尚未提交的动作在 _submit 处被拦下,
        # 缩小"检查 cancelled 通过 → 池子已关 → 提交"的竞态窗口。
        self._executor_closed = True
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass
        # executor 已完全停止, 此时释放 writer / 清理半成品才是安全的。
        self._close_writer()
        if not self.video_finalized:
            self._cleanup_video()


class VideoTaskManager:
    """视频任务注册表。

    资源治理(P0-1): 任务创建后若用户始终未连接 WebSocket(关闭页面 / 刷新 / 放弃检测),
    该任务与其上传的视频会永久驻留。此处通过 TTL + 容量上限两种机制自动回收僵尸任务,
    回收时释放线程池并删除其上传文件。
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, VideoDetectionTask] = {}
        self._lock = threading.Lock()

    def create(self, video_path: Path, source_name: str,
               conf_threshold: Optional[float] = None) -> VideoDetectionTask:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._reap_expired()
            self._evict_for_capacity()
            task = VideoDetectionTask(task_id, video_path, source_name, conf_threshold)
            self._tasks[task_id] = task
            return task

    def get(self, task_id: str) -> Optional[VideoDetectionTask]:
        return self._tasks.get(task_id)

    def remove(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task:
            task.shutdown()

    def _reap_expired(self) -> int:
        """回收超过 TTL 且未在运行的僵尸任务(需持锁调用)。

        TTL 设有下限 30s: 视频上传与元信息探测本身耗时数秒, 过小的配置会让
        正常任务刚创建即被回收, 属于误配保护而非可配置项。
        """
        ttl = max(30, int(settings.video_task_ttl_sec))
        now = time.time()
        expired = [
            tid for tid, t in self._tasks.items()
            if not t.running and (now - t.created_at) > ttl
        ]
        count = 0
        for tid in expired:
            task = self._tasks.pop(tid, None)
            if task is None:
                continue
            task.shutdown()
            task.delete_source()
            count += 1
        if count:
            logger.info("回收 %d 个超时未开始的视频任务(TTL=%ds)", count, ttl)
        return count

    def _evict_for_capacity(self) -> int:
        """超出驻留上限时挤掉最久未运行的任务(需持锁调用)。"""
        limit = max(1, int(settings.max_active_video_tasks))
        overflow = len(self._tasks) - (limit - 1)
        if overflow <= 0:
            return 0

        idle = sorted(
            (t for t in self._tasks.values() if not t.running),
            key=lambda x: x.created_at,
        )
        count = 0
        for victim in idle[:overflow]:
            self._tasks.pop(victim.task_id, None)
            victim.shutdown()
            victim.delete_source()
            count += 1
        if count:
            logger.info("任务数达上限(%d), 淘汰 %d 个滞留任务", limit, count)
        return count

    def reap(self) -> int:
        """外部触发一次回收(服务启动时清理历史残留)。"""
        with self._lock:
            return self._reap_expired()

    def active_count(self) -> int:
        """当前驻留的任务数(含未开始检测的)。"""
        return len(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)


# 全局任务管理器
task_manager = VideoTaskManager()
