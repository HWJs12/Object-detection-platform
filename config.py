"""
全局配置中心
================================
所有可调参数集中在本文件, 不要在其它模块中硬编码路径或阈值。

配置优先级(由高到低):
    1. 环境变量        (例: MODEL_PATH=models/best.pt)
    2. 项目根 config.yaml
    3. 本文件中的默认值

模型路径说明:
    若未显式指定 MODEL_PATH, 系统会自动在 models/ 目录下查找第一个 .pt 文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR: Path = Path(__file__).resolve().parent
CONFIG_FILE: Path = BASE_DIR / "config.yaml"


def _default_model_path() -> Optional[str]:
    """在 models/ 下自动发现权重文件(未显式配置时使用)。"""
    models_dir = BASE_DIR / "models"
    if not models_dir.exists():
        return None
    pts = sorted(models_dir.glob("*.pt"))
    return pts[0].as_posix() if pts else None


@dataclass
class Settings:
    # ================= 路径配置 =================
    base_dir: Path = BASE_DIR
    models_dir: Path = BASE_DIR / "models"
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    result_dir: Path = BASE_DIR / "data" / "results"
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = BASE_DIR / "data" / "app.db"
    model_path: Optional[str] = field(default_factory=_default_model_path)

    # ================= 推理参数 =================
    conf_threshold: float = 0.25      # 置信度阈值
    iou_threshold: float = 0.45       # NMS IoU 阈值
    imgsz: int = 640                  # 模型输入尺寸
    device: str = "auto"              # auto / cpu / cuda / 0
    max_det: int = 300                # 单张最大检测数
    half: bool = False                # CPU 下不使用半精度

    # ================= 目标跟踪参数(视频) =================
    tracker: str = "bytetrack.yaml"   # 跟踪器配置: bytetrack.yaml / botsort.yaml
    track_enabled: bool = True        # 视频检测是否启用目标跟踪(跨帧去重统计唯一目标)

    # ================= 结果导出 =================
    save_result_video: bool = True    # 视频检测时同步导出带检测框的结果视频(MP4)

    # ================= 视频检测参数 =================
    frame_skip: int = 2               # 每 N 帧检测 1 帧(CPU 提速)
    video_max_side: int = 960         # 送入推理前长边缩放上限
    jpeg_quality: int = 70            # WebSocket 推流 JPEG 质量
    push_fps_limit: float = 20.0      # 推流帧率上限, 0 表示不限
    frame_stats_interval: float = 0.5  # 视频逐帧统计采样间隔(秒)

    # ================= 服务配置 =================
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = False
    max_upload_mb: int = 300          # 单个上传文件大小上限
    max_batch_images: int = 20        # 批量图片检测单次最大张数

    # ================= 资源治理 =================
    # 僵尸任务回收: 上传后未连接 WebSocket(用户离开/刷新)的任务会永久滞留,
    # 超过 TTL 或超出数量上限时自动回收并删除其上传文件。
    max_active_video_tasks: int = 3    # 同时驻留的视频任务上限(含未开始的)
    video_task_ttl_sec: int = 600      # 任务未连接 WebSocket 的存活时间(秒)

    # 文件保留策略: 按天数清理过期的上传/结果文件, 并同步删除关联数据库记录
    # 论文素材需长期留存, 保留期设为 365 天(约等于不清理); 设 0 可完全关闭自动清理
    auto_cleanup: bool = True          # 服务启动时执行一次过期清理
    file_retention_days: int = 365     # 文件保留天数, 0 表示不清理
    dedupe_upload: bool = True         # 按文件内容去重, 重复上传直接复用已有文件

    # ================= 文件类型白名单 =================
    allowed_image_ext: tuple = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")
    allowed_video_ext: tuple = (".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm")

    # ================= 类别显示名映射(可选) =================
    # 类别本身始终从模型(model.names)读取, 这里只是可选的"显示名"翻译。
    # 留空 = 直接显示模型原始类别名(如 hd / hx / hs / sb / other)
    # 例: {"hd": "海胆", "hx": "海星", "hs": "海参", "sb": "扇贝", "other": "其他"}
    class_name_map: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        # 确保关键目录存在(目录创建失败不应导致程序崩溃, 交由异常处理捕获)
        for p in (self.data_dir, self.upload_dir, self.result_dir, self.models_dir):
            try:
                Path(p).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    @property
    def model_path_abs(self) -> Optional[Path]:
        """权重文件绝对路径(相对路径按项目根目录解析)。"""
        if not self.model_path:
            return None
        p = Path(self.model_path)
        return p if p.is_absolute() else (BASE_DIR / p)

    @property
    def device_resolved(self) -> str:
        """解析实际推理设备: auto -> 有 GPU 用 GPU, 否则 CPU。"""
        if self.device and self.device != "auto":
            return self.device
        try:
            import torch  # 局部导入, 避免未安装 torch 时配置加载失败
            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def to_dict(self) -> Dict[str, Any]:
        """导出可序列化的配置(供前端展示)。"""
        data: Dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Path):
                v = v.as_posix()
            elif isinstance(v, tuple):
                v = list(v)
            data[f.name] = v
        data["device_resolved"] = self.device_resolved
        return data


# 环境变量 -> 配置字段 映射
_ENV_MAP: Dict[str, str] = {
    "MODEL_PATH": "model_path",
    "CONF_THRESHOLD": "conf_threshold",
    "IOU_THRESHOLD": "iou_threshold",
    "IMGSZ": "imgsz",
    "DEVICE": "device",
    "FRAME_SKIP": "frame_skip",
    "HOST": "host",
    "PORT": "port",
    "MAX_UPLOAD_MB": "max_upload_mb",
}


def _cast(value: Any, target_type: Any) -> Any:
    """按 dataclass 字段类型做简单类型转换。"""
    try:
        if target_type in (int, "int"):
            return int(value)
        if target_type in (float, "float"):
            return float(value)
        if target_type in (bool, "bool"):
            return str(value).lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError):
        return value
    return value


def load_settings() -> Settings:
    """加载配置: 默认值 -> config.yaml -> 环境变量。"""
    settings = Settings()

    # 1) config.yaml 覆盖
    if CONFIG_FILE.exists():
        try:
            import yaml  # type: ignore
            with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
                raw = yaml.safe_load(fp) or {}
            for key, value in raw.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)
        except Exception:
            # YAML 缺失或解析失败时使用默认值, 不阻断启动
            pass

    # 2) 环境变量覆盖
    type_map = {f.name: f.type for f in fields(settings)}
    for env_key, field_name in _ENV_MAP.items():
        if env_key in os.environ:
            setattr(settings, field_name, _cast(os.environ[env_key], type_map.get(field_name)))

    # 3) 重新创建目录(配置可能被覆盖)
    settings.__post_init__()
    return settings


# 全局单例
settings: Settings = load_settings()
