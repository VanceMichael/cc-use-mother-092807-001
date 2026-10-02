"""值班工作台：系统重启后继续呈现待办。

所有待办均来自持久化表，进程重启不丢失：
- 待复核测试批次；
- 待解除检修锁（含检修窗口信息）；
- 开放的承诺缺口提醒；
- 待独立审批的产能例外；
- 隔离区中编号冲突的材料。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .auth import Actor
from .capacity_exceptions import pending_exceptions
from .inbound import list_quarantine
from .operations import locked_locks
from .supply import open_gap_alerts
from .testing import pending_tests


def workbench(conn: sqlite3.Connection, actor: Actor) -> dict[str, Any]:
    return {
        "actor": {"id": actor.id, "name": actor.name, "department": actor.department},
        "pending_tests": _safe(lambda: pending_tests(conn, actor)),
        "maintenance_locks": _safe(lambda: locked_locks(conn, actor)),
        "gap_alerts": open_gap_alerts(conn, actor),
        "pending_exceptions": _safe(lambda: pending_exceptions(conn, actor)),
        "quarantined_materials": _safe(lambda: list_quarantine(conn, actor)),
    }


def _safe(call: Any) -> list[Any]:
    """不同岗位看到不同板块；无权板块静默为空，而不是让整个工作台失败。"""
    try:
        return call()
    except Exception:
        return []
