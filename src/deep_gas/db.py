"""SQLite 连接与建库。业务库为单文件，重启后工作台状态全部可恢复。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path = ":memory:", check_same_thread: bool = True) -> sqlite3.Connection:
    """打开业务库并保证 schema 就绪。

    内存库用于测试；文件库用于服务运行，进程重启后数据保留。
    API 服务跨线程共享连接时传 check_same_thread=False 并自行串行化写请求。
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    apply_schema(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """幂等执行 schema（全部使用 IF NOT EXISTS）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
