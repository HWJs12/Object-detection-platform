"""SQLite 连接管理: 建表 / 获取连接 / 事务上下文。"""

from __future__ import annotations

import atexit
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import settings

logger = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
_local = threading.local()
_CLOSE_HOOK_REGISTERED = False


def _connect() -> sqlite3.Connection:
    """创建连接(每线程一个), 行结果以 dict 形式返回。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL 自动 checkpoint 阈值: 累积到该页数后自动落盘。不设置时 SQLite 默认 1000 页,
    # 但若进程被强制终止(如 Ctrl+C / taskkill)则永不 checkpoint, -wal 文件会持续膨胀。
    conn.execute("PRAGMA wal_autocheckpoint = 512")  # 约 2MB
    return conn


def get_connection() -> sqlite3.Connection:
    """获取当前线程的数据库连接(复用)。"""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Cursor]:
    """业务事务支持嵌套: 内层只释放保存点, 不提前提交外层业务。"""
    conn = get_connection()
    nested = conn.in_transaction
    savepoint = "sp_" + uuid.uuid4().hex
    cur = conn.cursor()
    try:
        if nested:
            cur.execute(f"SAVEPOINT {savepoint}")
        else:
            cur.execute("BEGIN IMMEDIATE")
        yield cur
        if nested:
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
    except Exception:
        if nested:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.rollback()
        raise
    finally:
        cur.close()


# 旧库升级: 表结构新增列 (列名 -> 列定义)
_MIGRATIONS: dict[str, dict[str, str]] = {
    "survey_stations": {
        "map_id": "INTEGER NOT NULL DEFAULT 1 CHECK(map_id BETWEEN 1 AND 3)",
    },
    "detections": {
        "unique_objects": "INTEGER NOT NULL DEFAULT 0",
        "total_detections": "INTEGER NOT NULL DEFAULT 0",
        # 已建立的跟踪轨迹数: 与 unique_objects 的区别在于不要求跨帧去重成立,
        # 用于向用户解释"本次提供了什么粒度的追踪", 旧记录默认 0。
        "tracked_objects": "INTEGER NOT NULL DEFAULT 0",
        "result_video": "TEXT",
        "counting_mode": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
        "unique_count_valid": "INTEGER NOT NULL DEFAULT 0",
        "source_hash": "TEXT NOT NULL DEFAULT ''",
        "inference_snapshot": "TEXT NOT NULL DEFAULT '{}'",
        # 调查维度: 全部可空/空串, 旧记录天然属于"未标注站点"
        "site_id": "INTEGER REFERENCES survey_stations(id)",
        "site": "TEXT NOT NULL DEFAULT ''",
        "transect": "TEXT NOT NULL DEFAULT ''",
        "depth_m": "REAL",
        "survey_round": "TEXT NOT NULL DEFAULT ''",
        # 相对丰度: 旧记录无法回溯逐帧类别计数, 默认 0 / 空, 由读层据此标注"未记录"
        "maxn_per_class": "TEXT NOT NULL DEFAULT '{}'",
        "maxn_total": "INTEGER NOT NULL DEFAULT 0",
        "cpunx": "REAL",
    },
    "video_frame_stats": {
        "class_counts": "TEXT NOT NULL DEFAULT '{}'",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移: 为已存在的旧表补充新增列(SQLite 不支持 IF NOT EXISTS 加列)。"""
    for table, columns in _MIGRATIONS.items():
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        current = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in columns.items():
            if col not in current:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # 图片中的框数有确定口径; 旧视频的跟踪状态无法追溯, 不伪造有效轨迹数。
    conn.execute(
        "UPDATE detections SET counting_mode='image_objects', unique_count_valid=1, "
        "unique_objects=total_objects, total_detections=total_objects "
        "WHERE task_type='image' AND counting_mode='legacy_unknown'"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detections_source_hash ON detections(source_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detections_site ON detections(site)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detections_site_id ON detections(site_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stations_map_active ON survey_stations(map_id,active)")


def init_db() -> None:
    """初始化数据库(建表 + 增量迁移), 失败时向上抛出由调用方处理。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _SCHEMA_FILE.read_text(encoding="utf-8")
    conn = get_connection()
    conn.executescript(schema)
    _migrate(conn)
    conn.commit()
    # 启动时把上次遗留的 WAL 内容落盘并截断文件, 防止 -wal 无限膨胀
    _register_exit_hook()
    checkpoint()


def checkpoint(mode: str = "TRUNCATE") -> None:
    """执行一次 WAL checkpoint, 把 -wal 中的内容合并回主库。

    Args:
        mode: PASSIVE(不阻塞读写) / FULL(阻塞至完成) / TRUNCATE(落盘并截断 wal 文件)
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = get_connection()
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row is not None:
            logger.debug("WAL checkpoint: busy=%s log=%s checkpointed=%s",
                         row[0], row[1], row[2])
    except sqlite3.Error as exc:
        logger.warning("WAL checkpoint 失败(不影响运行): %s", exc)


def _register_exit_hook() -> None:
    """注册进程退出钩子, 保证 Ctrl+C / taskkill 等非常规退出也能落盘。

    FastAPI 的 lifespan 关闭路径依赖服务正常退出, 强杀进程时不会执行,
    因此这里额外用 atexit + SystemExit 兜底。
    """
    global _CLOSE_HOOK_REGISTERED
    if _CLOSE_HOOK_REGISTERED:
        return
    _CLOSE_HOOK_REGISTERED = True

    def _on_exit() -> None:
        try:
            logger.info("进程退出: 正在执行 WAL 落盘")
            checkpoint()
        except Exception:
            pass

    atexit.register(_on_exit)


def close_connection() -> None:
    """关闭当前线程连接(进程退出时调用)。"""
    try:
        checkpoint()
    except Exception:
        pass
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
