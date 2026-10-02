"""HTTP 服务器装配。"""

from __future__ import annotations

from http.server import HTTPServer
from pathlib import Path

from .api import ApiHandler
from .db import connect, init_db
from .workflow import ServiceHub


def build_hub(db_path: str | Path) -> ServiceHub:
    conn = connect(db_path)
    init_db(conn)
    return ServiceHub(conn)


def make_server(host: str, port: int, db_path: str | Path) -> HTTPServer:
    hub = build_hub(db_path)
    hub.recover()

    class Handler(ApiHandler):
        pass

    Handler.hub = hub
    # 单线程服务：所有读写经同一连接串行执行，避免跨线程事务与锁问题
    server = HTTPServer((host, port), Handler)
    server.hub = hub  # type: ignore[attr-defined]
    return server
