"""参与方身份、部门角色与操作授权。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import AuthorizationError, NotFound
from .util import now

# 部门：井队 / 地质 / 维护 / 输气 / 调度 / 供气计划 / 商业用户
DEPARTMENTS = frozenset({
    "drilling", "geology", "maintenance", "pipeline",
    "dispatch", "planning", "commercial",
})
INTERNAL_DEPARTMENTS = DEPARTMENTS - {"commercial"}


@dataclass(frozen=True)
class Actor:
    id: str
    name: str
    department: str

    @property
    def is_commercial(self) -> bool:
        return self.department == "commercial"


def register_actor(conn: sqlite3.Connection, actor_id: str, name: str, department: str) -> Actor:
    if department not in DEPARTMENTS:
        raise AuthorizationError(f"未知部门: {department}")
    conn.execute(
        "INSERT INTO actors(id, name, department, created_at) VALUES (?,?,?,?)",
        (actor_id, name, department, now()),
    )
    conn.commit()
    return Actor(actor_id, name, department)


def get_actor(conn: sqlite3.Connection, actor_id: str) -> Actor:
    row = conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
    if row is None:
        raise NotFound(f"参与方不存在: {actor_id}")
    return Actor(row["id"], row["name"], row["department"])


def require_departments(actor: Actor, departments: set[str] | frozenset[str], action: str) -> None:
    """要求操作人属于指定部门；商业用户被所有内部写操作排除。"""
    if actor.department not in departments:
        raise AuthorizationError(f"{actor.name}({actor.department}) 无权执行: {action}")


def reject_commercial(actor: Actor, action: str) -> None:
    if actor.is_commercial:
        raise AuthorizationError(f"商业用户无权访问内部生产数据: {action}")


def can_view_contract(conn: sqlite3.Connection, actor: Actor, contract_id: str) -> bool:
    """商业用户只能查看被授权合同；内部岗位默认可见。"""
    if not actor.is_commercial:
        return True
    row = conn.execute(
        "SELECT 1 FROM contract_audience WHERE contract_id = ? AND actor_id = ?",
        (contract_id, actor.id),
    ).fetchone()
    return row is not None


def require_contract_visibility(conn: sqlite3.Connection, actor: Actor, contract_id: str) -> None:
    if not can_view_contract(conn, actor, contract_id):
        raise AuthorizationError("该供气信息与你的合同无关")


def grant_contract(conn: sqlite3.Connection, contract_id: str, actor_id: str) -> None:
    """为商业用户登记合同可见授权。"""
    conn.execute(
        "INSERT OR IGNORE INTO contract_audience(contract_id, actor_id) VALUES (?,?)",
        (contract_id, actor_id),
    )
    conn.commit()


def grant_contract_by_code(conn: sqlite3.Connection, contract_code: str, actor_id: str) -> None:
    """按合同编号登记可见授权（供 API 层使用）。"""
    row = conn.execute("SELECT id FROM contracts WHERE code=?", (contract_code,)).fetchone()
    if row is None:
        raise NotFound(f"合同不存在: {contract_code}")
    grant_contract(conn, row["id"], actor_id)


def actor_public(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "department": row["department"]}
