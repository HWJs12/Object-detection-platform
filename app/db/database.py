"""SQLite 连接管理: 建表 / 获取连接 / 事务上下文。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import settings

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
_local = threading.local()


def _connect() -> sqlite3.Connection:
    """创建连接(每线程一个), 行结果以 dict 形式返回。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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
    """事务上下文: 成功自动提交, 异常自动回滚。"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def init_db() -> None:
    """初始化数据库(建表), 失败时向上抛出由调用方处理。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _SCHEMA_FILE.read_text(encoding="utf-8")
    conn = get_connection()
    conn.executescript(schema)
    conn.commit()


def close_connection() -> None:
    """关闭当前线程连接(进程退出时调用)。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
