-- 数据库表结构(SQLite)
-- 设计原则: 检测主表 / 类别统计表 / 明细表 / 视频帧统计表 分离, 不做大字段硬塞

PRAGMA foreign_keys = ON;

-- 检测主表: 一次图片或视频检测对应一条
CREATE TABLE IF NOT EXISTS detections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type         TEXT    NOT NULL,                  -- image | video
    source_name       TEXT    NOT NULL,                  -- 原始文件名
    saved_name        TEXT,                              -- 服务器保存名
    result_image      TEXT,                              -- 结果图相对路径(图片检测)
    result_video      TEXT,                              -- 结果视频文件名(视频检测, 带检测框)
    created_at        TEXT    NOT NULL,                  -- YYYY-MM-DD HH:MM:SS
    total_objects     INTEGER NOT NULL DEFAULT 0,        -- 目标总数
    image_width       INTEGER,
    image_height      INTEGER,
    inference_ms      REAL,                              -- 推理耗时(毫秒)
    frames_processed  INTEGER,                           -- 已处理帧数(视频)
    avg_fps           REAL,                              -- 平均处理速度(视频)
    duration_sec      REAL,                              -- 视频时长(秒)
    conf_threshold    REAL,                              -- 本次使用的置信度阈值
    unique_objects    INTEGER NOT NULL DEFAULT 0,        -- 唯一目标数(目标跟踪跨帧去重)
    total_detections  INTEGER NOT NULL DEFAULT 0,        -- 检测框累计数(所有帧求和)
    status            TEXT    NOT NULL DEFAULT 'success',-- success | failed
    message           TEXT                               -- 备注 / 错误信息
);

CREATE INDEX IF NOT EXISTS idx_detections_created_at ON detections(created_at);
CREATE INDEX IF NOT EXISTS idx_detections_type       ON detections(task_type);

-- 检测明细表: 图片检测每个目标一条; 视频检测保存采样帧的目标
CREATE TABLE IF NOT EXISTS detection_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id INTEGER NOT NULL,
    frame_index  INTEGER NOT NULL DEFAULT 0,
    class_id     INTEGER NOT NULL,
    class_name   TEXT    NOT NULL,
    confidence   REAL    NOT NULL,
    x1           REAL,
    y1           REAL,
    x2           REAL,
    y2           REAL,
    FOREIGN KEY (detection_id) REFERENCES detections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_detection ON detection_items(detection_id);

-- 类别统计表: 一次检测中各分类的目标数量
CREATE TABLE IF NOT EXISTS detection_class_stats (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id INTEGER NOT NULL,
    class_id     INTEGER NOT NULL,
    class_name   TEXT    NOT NULL,
    object_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (detection_id) REFERENCES detections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_class_stats_detection ON detection_class_stats(detection_id);

-- 视频逐帧统计表: 帧号 / 时间戳 / 当前帧目标数
CREATE TABLE IF NOT EXISTS video_frame_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id  INTEGER NOT NULL,
    frame_index   INTEGER NOT NULL,
    timestamp_sec REAL    NOT NULL DEFAULT 0,
    total_objects INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (detection_id) REFERENCES detections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_frame_stats_detection ON video_frame_stats(detection_id);
