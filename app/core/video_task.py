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
import json
import math
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
from app.core.video_encoder import H264VideoWriter
from app.core.provenance import file_sha256, inference_snapshot
from app.db import dao
from app.utils.exceptions import AppError, InvalidFileError
from app.utils.file_utils import release_upload, retain_file
from app.utils.image_utils import (
    MAX_DECODE_PIXELS,
    draw_detections,
    resize_long_side,
    to_base64,
)
from app.utils.survey import normalize_survey
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
    unique_objects: Optional[int] = None
    counting_mode: str = "frame_detections"
    unique_count_valid: bool = False
    warnings: List[str] = field(default_factory=list)
    counts: List[Dict[str, Any]] = field(default_factory=list)


class VideoDetectionTask:
    """一次视频检测任务。"""

    MAX_SAMPLE_ITEMS = 200  # 入库明细采样上限, 避免数据库膨胀

    def __init__(self, task_id: str, video_path: Path, source_name: str,
                 conf_threshold: Optional[float] = None,
                 survey: Optional[Dict[str, Any]] = None) -> None:
        self.task_id = task_id
        self.video_path = video_path
        self.source_name = source_name
        # 调查维度(站点/样方/水深/调查轮次): 与检测结果一同落库, 不参与推理。
        self.survey = normalize_survey(survey)
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
        self._result_lease = False
        # 资源安全: 线程池关闭标志。shutdown() 与 run() 的提交动作分处两个线程,
        # 存在"检查 cancelled 通过 → 池子被关闭 → 提交"的竞态窗口, 故单独设标志
        # 供 _submit 判定, 关闭后一律拒绝提交而不抛异常。
        self._executor_closed = False
        # 资源治理: 记录创建时间, 供僵尸任务(上传后未连接 WebSocket)回收使用
        self.created_at: float = time.time()
        self.running: bool = False        # 是否正在执行, 运行中的任务不回收
        self._claimed = False
        self._claim_lock = threading.Lock()
        # 本任务使用的置信度阈值(创建时指定, 运行中可通过控制接口实时调整)
        self.conf_threshold = float(
            settings.conf_threshold if conf_threshold is None else conf_threshold
        )
        # A running job owns an immutable set of processing/output parameters.
        # Only confidence can change through its explicit task control endpoint.
        self.parameters = {
            "conf_threshold": self.conf_threshold,
            "iou_threshold": settings.iou_threshold,
            "imgsz": settings.imgsz,
            "max_det": settings.max_det,
            "device": settings.device_resolved,
            "frame_skip": max(1, int(settings.frame_skip)),
            "video_max_side": settings.video_max_side,
            "jpeg_quality": settings.jpeg_quality,
            "push_fps_limit": settings.push_fps_limit,
            "frame_stats_interval": settings.frame_stats_interval,
            "track_enabled": bool(settings.track_enabled),
            "tracker": settings.tracker,
            "save_result_video": self._save_video,
        }
        self.confidence_changes: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self._tracking_failed = not self.parameters["track_enabled"]
        self._had_tracked = False
        self._warned_partial = False
        if not self.parameters["track_enabled"]:
            self.warnings.append("本任务未启用跨帧关联，计数采用单帧峰值（MaxN）与逐帧检出框口径")
        # 运行状态(供控制接口查询)
        self.frame_index = 0
        self.processed = 0
        self._paused_seconds = 0.0
        self._pause_started = 0.0
        self._last_pause_heartbeat = 0.0
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
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            if not math.isfinite(fps) or fps <= 0:
                fps = 25.0
                self._warn("视频缺少有效帧率，时间轴暂按 25 FPS 估算")
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if width > 0 and height > 0 and width * height > MAX_DECODE_PIXELS:
                raise InvalidFileError(
                    f"视频分辨率超过安全上限 ({MAX_DECODE_PIXELS:,} 像素)"
                )
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
    def _process_frame(self, frame: np.ndarray, conf: Optional[float] = None):
        """缩放 -> 跟踪推理 -> 编码原帧, 返回 (base64, 明细, 推理耗时ms)。

        注意: 返回的是**未绘制检测框的原始帧**。检测框改由前端 Canvas 绘制,
        这样才能支持按类别实时筛选、显示 track_id, 并省去服务端绘制开销。
        """
        small = resize_long_side(frame, self.parameters["video_max_side"])
        started = time.perf_counter()
        used_conf = self.conf_threshold if conf is None else conf
        kwargs = {"conf": used_conf, "iou": self.parameters["iou_threshold"],
                  "imgsz": self.parameters["imgsz"], "max_det": self.parameters["max_det"],
                  "device": self.parameters["device"]}
        warning = None
        tracking_valid = False
        if self.parameters["track_enabled"] and not self._tracking_failed:
            detections, tracking_valid, warning = engine.track_with_status(
                small, tracker=self.parameters["tracker"], **kwargs)
        else:
            detections = engine.predict(small, **kwargs)
        infer_ms = (time.perf_counter() - started) * 1000
        if self._save_video:
            # 结果视频: 在检测帧上绘制检测框后写入
            # 输出帧率按跳帧折算(fps/frame_skip), 保证成片时长与原视频一致
            self._write_frame(draw_detections(small.copy(), detections))
        return (to_base64(small, self.parameters["jpeg_quality"]),
                [d.to_dict() for d in detections], infer_ms, tracking_valid, warning)

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def counting_mode(self) -> str:
        if not self._tracking_failed:
            return "tracked_objects"
        return "tracking_partial" if self._had_tracked else "frame_detections"

    @property
    def counting_mode_reason(self) -> str:
        """把内部状态翻译成中性、可解释的原因说明, 不评价能力高低。

        措辞原则: 只说"本次采用了哪套口径、依据是什么", 不使用"失效/不足/失败"
        这类评价性词汇, 以免把一个口径选择读成系统缺陷。

        注意: 该方法只用于"未取用轨迹数"的场景(见 summary_text / done 消息)。
        跨帧关联成立时口径是轨迹去重, 不存在"缺少依据"这回事,
        因此这里明确返回空串, 由调用方决定是否展示, 避免把正常结果说成有缺陷。
        """
        if not self._tracking_failed:
            return ""
        if not self.parameters["track_enabled"]:
            return "本任务未启用跨帧关联"
        if self._had_tracked:
            return "部分目标在相邻帧间未续上编号, 跨帧去重缺少完整依据"
        return "本次视频中目标在帧间未形成稳定关联"

    @property
    def unique_count_valid(self) -> bool:
        return not self._tracking_failed

    def claim(self) -> None:
        """Atomically reserve a task for exactly one WebSocket owner."""
        with self._claim_lock:
            if self._claimed or self.cancelled or self._executor_closed:
                raise AppError("该视频任务已连接或已结束，请勿重复连接", code="TASK_BUSY", status_code=409)
            self._claimed = True

    @property
    def claimed(self) -> bool:
        return self._claimed

    # ---------------- 结果视频 ----------------
    def _write_frame(self, frame_bgr: np.ndarray) -> None:
        """写入一帧带检测框的画面(懒创建 VideoWriter)。"""
        try:
            if self._writer is None:
                if not self._result_lease:
                    retain_file(self.result_video_path)
                    self._result_lease = True
                height, width = frame_bgr.shape[:2]
                out_fps = max(0.01, self.meta.fps / self.parameters["frame_skip"])
                self._writer = H264VideoWriter(self.result_video_path, out_fps, (width, height))
            self._writer.write(frame_bgr)
        except Exception as exc:  # 导出失败不影响检测主流程
            self._save_video = False
            self._warn("结果视频导出失败，本次保留检测统计；请检查 H.264 编码器与可用磁盘空间")
            logger.warning("结果视频写入失败, 已停止导出: %s", exc)

    def _close_writer(self) -> None:
        """释放视频写入器(释放后文件才完整)。"""
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception as exc:
                self._save_video = False
                self._warn("结果视频未能完整编码，本次未提供结果视频；检测统计仍已保留")
                logger.warning("结果视频编码失败: %s", exc)
            self._writer = None

    def _cleanup_video(self) -> None:
        """删除未被采纳的结果视频(任务取消 / 异常时)。"""
        try:
            self._close_writer()
            self.result_video_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _release_result_lease(self) -> None:
        if self._result_lease:
            release_upload(self.result_video_path)
            self._result_lease = False

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
            future = loop.run_in_executor(self._executor, fn, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # Native OpenCV/model work cannot be cancelled by cancelling its
                # asyncio wrapper. Drain it before releasing capture/writer/slot.
                await asyncio.shield(future)
                raise
        except RuntimeError as exc:
            if "cannot schedule new futures" in str(exc):
                return _CANCELLED
            raise

    # ---------------- 主流程 ----------------
    async def _wait_until_resumed(self, send) -> None:
        while self.paused and not self.cancelled:
            now = time.perf_counter()
            if now - self._last_pause_heartbeat >= 3.0:
                await send({"type": "paused", "frame_index": self.frame_index,
                            "total_frames": self.meta.total_frames})
                self._last_pause_heartbeat = now
            await asyncio.sleep(0.1)

    async def run(self, send: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Process a claimed task, keeping source positions and count semantics explicit."""
        if self.running:
            raise AppError("视频任务正在运行，请勿重复启动", code="TASK_BUSY", status_code=409)
        if not self.claimed:
            self.claim()
        use_tracking = self.parameters["track_enabled"]
        engine.begin_video(tracking=use_tracking)
        self.running = True
        cap = None
        loop = asyncio.get_running_loop()
        try:
            cap = await self._submit(loop, cv2.VideoCapture, str(self.video_path))
            if cap is _CANCELLED or not cap.isOpened():
                raise InvalidFileError("视频无法打开，请确认文件未损坏且编码受支持")
            if use_tracking:
                engine.reset_tracker()
            model_metadata = await self._submit(loop, engine.model_provenance, use_tracking)
            snapshot = inference_snapshot({**self.parameters, "model": model_metadata})
            frame_skip = self.parameters["frame_skip"]
            fps_limit = self.parameters["push_fps_limit"]
            min_interval = 1.0 / fps_limit if fps_limit > 0 else 0.0
            frame_index = processed = total_detections = peak_objects = 0
            total_infer_ms = 0.0
            tracked: Dict[int, Dict[str, Any]] = {}
            tracked_counter: Dict[str, int] = {}
            frame_counter: Dict[str, int] = {}
            class_id_of: Dict[str, int] = {}
            frame_rows: List[Tuple[int, float, int, Dict[str, int]]] = []
            # MaxN: 逐帧按类别取峰值。与跟踪无关, 因此跟踪失效时仍成立。
            maxn_per_class: Dict[str, int] = {}
            sample_items: List[Dict[str, Any]] = []
            started_at = time.perf_counter()
            last_push_at = 0.0
            last_sample_ts = -1.0
            try:
                while not self.cancelled:
                    await self._wait_until_resumed(send)
                    if self.cancelled:
                        break
                    read_out = await self._submit(loop, cap.read)
                    if read_out is _CANCELLED:
                        self.cancelled = True
                        break
                    ret, frame = read_out
                    if not ret:
                        if self.meta.total_frames and frame_index < self.meta.total_frames - 1:
                            self._warn("视频提前结束或部分帧无法解码，统计仅覆盖实际读取的片段")
                        break
                    current_index = frame_index
                    # Every successful read consumes one source frame, including
                    # frames skipped for inference and pauses during in-flight work.
                    frame_index += 1
                    self.frame_index = frame_index
                    if frame.shape[0] * frame.shape[1] > MAX_DECODE_PIXELS:
                        raise InvalidFileError(f"视频帧分辨率超过安全上限 ({MAX_DECODE_PIXELS:,} 像素)")
                    if current_index % frame_skip:
                        continue
                    await self._wait_until_resumed(send)
                    if self.cancelled:
                        break
                    used_conf = self.conf_threshold
                    frame_out = await self._submit(loop, self._process_frame, frame, used_conf)
                    if frame_out is _CANCELLED:
                        self.cancelled = True
                        break
                    image_b64, items, infer_ms, tracking_valid, warning = frame_out
                    if warning:
                        self._warn(warning)
                    if not tracking_valid:
                        self._tracking_failed = True
                    processed += 1
                    self.processed = processed
                    total_infer_ms += infer_ms
                    total_in_frame = len(items)
                    total_detections += total_in_frame
                    peak_objects = max(peak_objects, total_in_frame)
                    counts: Dict[str, int] = {}
                    for item in items:
                        name = item["class_name"]
                        counts[name] = counts.get(name, 0) + 1
                        frame_counter[name] = frame_counter.get(name, 0) + 1
                        class_id_of.setdefault(name, item["class_id"])
                        tid = item.get("track_id")
                        if tid is None:
                            self._tracking_failed = True
                            continue
                        self._had_tracked = True
                        record = tracked.get(tid)
                        if record is None:
                            tracked[tid] = {"class_name": name, "frames": 1,
                                            "max_conf": item["confidence"]}
                            tracked_counter[name] = tracked_counter.get(name, 0) + 1
                        else:
                            record["frames"] += 1
                            record["max_conf"] = max(record["max_conf"], item["confidence"])
                    # MaxN 只要同一帧内不重复计数即可, 与跨帧关联成败无关。
                    for name, count in counts.items():
                        if count > maxn_per_class.get(name, 0):
                            maxn_per_class[name] = count
                    if not self.unique_count_valid and use_tracking and not self._warned_partial:
                        self._warned_partial = True
                        self._warn(
                            "跨帧关联未获完整依据，本次改以单帧峰值（MaxN）与逐帧检出框展示；"
                            "两者均为相对丰度，不等同于生物个体数"
                        )
                    timestamp = current_index / self.meta.fps if self.meta.fps > 0 else 0.0
                    counts_list = [{"class_name": name, "count": count} for name, count in
                                   sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
                    if timestamp - last_sample_ts >= self.parameters["frame_stats_interval"]:
                        last_sample_ts = timestamp
                        frame_rows.append((current_index, round(timestamp, 2), total_in_frame, dict(counts)))
                        remaining = self.MAX_SAMPLE_ITEMS - len(sample_items)
                        sample_items.extend({**item, "frame_index": current_index} for item in items[:remaining])
                    # Keep the already processed frame during a pause. It remains
                    # counted and exported, then is pushed when processing resumes.
                    await self._wait_until_resumed(send)
                    if self.cancelled:
                        break
                    now = time.perf_counter()
                    if now - last_push_at >= min_interval:
                        last_push_at = now
                        elapsed = now - started_at - self._paused_seconds
                        await send({
                            "type": "frame", "frame_index": current_index,
                            "timestamp": round(timestamp, 2), "total": total_in_frame,
                            "unique": len(tracked) if self.unique_count_valid else None,
                            "unique_count_valid": self.unique_count_valid,
                            "counting_mode": self.counting_mode, "warnings": list(self.warnings),
                            "counts": counts_list, "detections": items, "image": image_b64,
                            "fps": round(processed / elapsed, 2) if elapsed > 0 else 0.0,
                            "conf_threshold": used_conf,
                            "progress": round(min(100, frame_index / max(self.meta.total_frames, 1) * 100), 1),
                        })
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            finally:
                # _submit drains native work on cancellation before these run.
                await loop.run_in_executor(None, cap.release)
                cap = None
                await loop.run_in_executor(None, self._close_writer)
            if self.cancelled:
                await loop.run_in_executor(None, self._cleanup_video)
                return
            if processed == 0:
                raise InvalidFileError("视频没有可处理的帧，未保存空的成功记录")
            elapsed = time.perf_counter() - started_at - self._paused_seconds
            avg_fps = round(processed / elapsed, 2) if elapsed > 0 else 0.0
            avg_objects = round(total_detections / processed, 2)
            result_video_name = None
            if self._save_video and self.result_video_path.exists() and self.result_video_path.stat().st_size > 0:
                result_video_name = self.result_video_name
            elif self.parameters["save_result_video"]:
                await loop.run_in_executor(None, self._cleanup_video)
            unique_objects = len(tracked) if self.unique_count_valid else None
            # ---- MaxN 相对丰度 ----
            # 与 tracking 无关的口径, 保证"唯一计数不可用"时仍有可解释的数量。
            # 必须先算出来: 下面的 class_counter 口径要跟随 total_objects 的口径。
            maxn_per_class = dict(sorted(maxn_per_class.items(),
                                         key=lambda kv: (-kv[1], kv[0])))
            maxn_total = max(maxn_per_class.values()) if maxn_per_class else 0
            # class_counter 必须与 record["total_objects"] 同口径, 否则统计页按类别
            # 汇总 detection_class_stats 时会得到比 total_objects 大几个数量级的数:
            # 本视频实测逐帧累计 2332 vs MaxN 10。三种口径一一对应:
            #   轨迹完整   -> tracked_counter (唯一轨迹数)   == total_objects=unique_objects
            #   部分失效   -> maxn_per_class (单帧类别峰值)  == total_objects=maxn_total
            #   全部失效   -> frame_counter (逐帧累计)      == total_objects=total_detections
            if self.unique_count_valid:
                class_counter = tracked_counter
            elif maxn_total > 0:
                class_counter = maxn_per_class
            else:
                class_counter = frame_counter
            counts_summary = [{"class_name": name, "count": count} for name, count in
                              sorted(class_counter.items(), key=lambda kv: (-kv[1], kv[0]))]
            duration_h = (self.meta.duration_sec / 3600.0) if self.meta.duration_sec > 0 else 0.0
            cpunx = round(maxn_total / duration_h, 1) if duration_h > 0 and maxn_total else None
            snapshot["confidence_changes"] = list(self.confidence_changes)
            snapshot["counting_mode"] = self.counting_mode
            snapshot["warnings"] = list(self.warnings)
            snapshot["source_frames_read"] = frame_index
            snapshot["maxn_per_class"] = maxn_per_class
            snapshot["maxn_total"] = maxn_total
            source_hash = await self._submit(loop, file_sha256, self.video_path)
            created_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 叙事口径: 不再用"不可用"作结论, 而是说明本次采用了哪套可解释的口径。
            if self.unique_count_valid:
                summary_text = f"跟踪轨迹 {unique_objects} 条"
                maxn_text = f"单类别峰值 MaxN {maxn_total} 条" if maxn_total else ""
            elif maxn_total > 0:
                summary_text = (f"跨帧去重未获完整依据，本次采用单帧峰值 MaxN {maxn_total} 条"
                                f"（{self.counting_mode_reason}）")
                # 口径已在 summary_text 中说明, 不再重复拼接 MaxN 字段与同类告警
                maxn_text = ""
            else:
                summary_text = f"本次未检出目标，累计检测框 {total_detections} 个"
                maxn_text = ""
            record = {
                "task_type": "video", "source_name": self.source_name,
                "saved_name": self.video_path.name, "result_video": result_video_name,
                "created_at": created_at,
                # total_objects 统一存"最可用的那个数", 与读层 effective_count 保持一致
                "total_objects": (unique_objects if self.unique_count_valid
                                  else (maxn_total if maxn_total > 0 else total_detections)),
                "unique_objects": unique_objects if self.unique_count_valid else 0,
                "total_detections": total_detections,
                # 跟踪轨迹数单独保留: 即使去重依据不完整, 也如实记录"本次建立了多少条轨迹",
                # 供前端展示"可提供的追踪粒度", 而不是把这一列当成 0 或 NULL 丢弃。
                "tracked_objects": len(tracked) if self._had_tracked else 0,
                "maxn_per_class": maxn_per_class, "maxn_total": maxn_total, "cpunx": cpunx,
                "counting_mode": self.counting_mode, "unique_count_valid": int(self.unique_count_valid),
                "source_hash": source_hash, "inference_snapshot": json.dumps(snapshot, ensure_ascii=False),
                **self.survey,
                "image_width": self.meta.width, "image_height": self.meta.height,
                "inference_ms": round(total_infer_ms / processed, 1), "frames_processed": processed,
                "avg_fps": avg_fps, "duration_sec": self.meta.duration_sec,
                "conf_threshold": self.parameters["conf_threshold"], "status": "success",
                # 两个峰值含义不同, 必须写明, 否则"单帧峰值 2"与 maxn_total=1 会被读成矛盾:
                #   peak_objects  = 单帧所有类别合计的最大值(该帧有多少个框)
                #   maxn_total    = 单个类别在同一帧内的最大值(相对丰度指标)
                "message": ("；".join(part for part in (
                    summary_text,
                    f"累计检测框 {total_detections} 个",
                    f"单帧总目标峰值 {peak_objects} 个",
                    maxn_text,
                    # 跨帧依据不完整时, 告警内容与 summary_text 语义重复, 不再拼接
                    "；".join(self.warnings) if self.unique_count_valid else "",
                ) if part)),
            }
            class_stats = [(class_id_of.get(name, -1), name, count) for name, count in class_counter.items()]
            record_id = await self._submit(loop, dao.save_detection, record, sample_items, class_stats, frame_rows)
            self.video_finalized = True
            self.result = VideoTaskResult(
                record_id=record_id, frames_processed=processed, total_frames=self.meta.total_frames,
                duration_sec=self.meta.duration_sec, avg_fps=avg_fps, peak_objects=peak_objects,
                avg_objects=avg_objects, total_detections=total_detections,
                unique_objects=unique_objects, counting_mode=self.counting_mode,
                unique_count_valid=self.unique_count_valid, warnings=list(self.warnings), counts=counts_summary,
            )
            await send({
                "type": "done", "record_id": record_id,
                "result_video_url": f"/data/results/{result_video_name}" if result_video_name else None,
                "warnings": list(self.warnings),
                "summary": {
                    "frames_processed": processed, "conf_threshold": self.conf_threshold,
                    "total_frames": self.meta.total_frames, "duration_sec": self.meta.duration_sec,
                    "avg_fps": avg_fps, "peak_objects": peak_objects, "avg_objects": avg_objects,
                    "total_detections": total_detections, "unique_objects": unique_objects,
                    "unique_count_valid": self.unique_count_valid, "counting_mode": self.counting_mode,
                    "counting_mode_reason": self.counting_mode_reason,
                    "maxn_total": maxn_total, "maxn_per_class": maxn_per_class, "cpunx": cpunx,
                    "warnings": list(self.warnings), "counts": counts_summary,
                },
            })
        finally:
            try:
                if cap is not None:
                    await loop.run_in_executor(None, cap.release)
                if not self.video_finalized:
                    await loop.run_in_executor(None, self._cleanup_video)
            finally:
                self._release_result_lease()
                self.running = False
                engine.end_video(tracking=use_tracking)

    # ---------------- 运行控制(暂停 / 继续 / 调整阈值) ----------------
    def pause(self) -> None:
        """暂停检测: 停止读帧与推流, 保持当前播放位置。"""
        if self.paused:
            return
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
        if conf != self.conf_threshold:
            self.confidence_changes.append({
                "effective_from_frame": self.frame_index,
                "previous": self.conf_threshold,
                "value": conf,
            })
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
            "counting_mode": self.counting_mode,
            "unique_count_valid": self.unique_count_valid,
            "warnings": list(self.warnings),
            "parameters": dict(self.parameters),
        }

    def cancel(self) -> None:
        """请求取消任务。"""
        self.cancelled = True

    def delete_source(self) -> None:
        """释放源文件租约; 只在无活动任务且没有历史记录引用时删除。"""
        release_upload(self.video_path, discard_if_unreferenced=True)

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
        self._release_result_lease()


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
               conf_threshold: Optional[float] = None,
               survey: Optional[Dict[str, Any]] = None) -> VideoDetectionTask:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._reap_expired()
            self._evict_for_capacity()
            if len(self._tasks) >= max(1, int(settings.max_active_video_tasks)):
                raise AppError("视频任务已达上限，请等待已有任务结束", code="VIDEO_CAPACITY", status_code=409)
            task = VideoDetectionTask(task_id, video_path, source_name, conf_threshold, survey)
            self._tasks[task_id] = task
            return task

    def get(self, task_id: str) -> Optional[VideoDetectionTask]:
        return self._tasks.get(task_id)

    def claim(self, task_id: str) -> Optional[VideoDetectionTask]:
        """Claim while holding the registry lock, excluding TTL/capacity eviction."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.claim()
            return task

    def remove(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task:
            task.shutdown()
            if task.video_finalized:
                release_upload(task.video_path)
            else:
                task.delete_source()

    def _reap_expired(self) -> int:
        """回收超过 TTL 且未在运行的僵尸任务(需持锁调用)。

        TTL 设有下限 30s: 视频上传与元信息探测本身耗时数秒, 过小的配置会让
        正常任务刚创建即被回收, 属于误配保护而非可配置项。
        """
        ttl = max(30, int(settings.video_task_ttl_sec))
        now = time.time()
        expired = [
            tid for tid, t in self._tasks.items()
            if not t.running and not t.claimed and (now - t.created_at) > ttl
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
            (t for t in self._tasks.values() if not t.running and not t.claimed),
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
