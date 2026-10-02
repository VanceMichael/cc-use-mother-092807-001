"""目录服务：区块、井、储层解释版本、外输通道与路由、商业合同。"""

from __future__ import annotations

import sqlite3
from typing import Any

from .auth import Actor, reject_commercial, require_departments
from .errors import Conflict, NotFound, ValidationError
from .util import new_id, now


def create_block(conn: sqlite3.Connection, actor: Actor, code: str, name: str) -> dict[str, Any]:
    reject_commercial(actor, "登记区块")
    if not code.strip() or not name.strip():
        raise ValidationError("区块编号与名称不能为空")
    block_id = new_id("blk")
    conn.execute(
        "INSERT INTO blocks(id, code, name, created_at) VALUES (?,?,?,?)",
        (block_id, code.strip(), name.strip(), now()),
    )
    conn.commit()
    return {"id": block_id, "code": code, "name": name}


def create_well(conn: sqlite3.Connection, actor: Actor, block_code: str, code: str, name: str) -> dict[str, Any]:
    """登记新井。大修井复用原井记录，通过作业方案表达复产，不另建井。"""
    require_departments(actor, {"drilling", "geology", "dispatch"}, "登记井")
    block = conn.execute("SELECT * FROM blocks WHERE code = ?", (block_code,)).fetchone()
    if block is None:
        raise NotFound(f"区块不存在: {block_code}")
    if conn.execute("SELECT 1 FROM wells WHERE code = ?", (code,)).fetchone():
        raise Conflict(f"井号已存在: {code}")
    well_id = new_id("well")
    conn.execute(
        "INSERT INTO wells(id, block_id, code, name, status, created_at) VALUES (?,?,?,?, 'drilling', ?)",
        (well_id, block["id"], code.strip(), name.strip(), now()),
    )
    conn.commit()
    return {"id": well_id, "block_id": block["id"], "code": code, "name": name, "status": "drilling"}


def get_well(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM wells WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"井不存在: {code}")
    return row


def submit_reservoir_version(
    conn: sqlite3.Connection, actor: Actor, well_code: str, formation: str,
    interpretation: str,
) -> dict[str, Any]:
    """提交储层解释版本；同一井同一层系版本号只能递增。"""
    require_departments(actor, {"geology"}, "提交储层解释")
    well = get_well(conn, well_code)
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS maxv FROM reservoir_versions "
        "WHERE well_id = ? AND formation = ?",
        (well["id"], formation),
    ).fetchone()
    version_no = row["maxv"] + 1
    rid = new_id("res")
    conn.execute(
        "INSERT INTO reservoir_versions(id, well_id, formation, version_no, interpretation, "
        "submitted_by, created_at) VALUES (?,?,?,?,?,?,?)",
        (rid, well["id"], formation, version_no, interpretation, actor.id, now()),
    )
    conn.commit()
    return {"id": rid, "well_id": well["id"], "formation": formation,
            "version_no": version_no, "interpretation": interpretation}


def latest_reservoir(conn: sqlite3.Connection, well_id: str, formation: str | None = None) -> sqlite3.Row | None:
    if formation is not None:
        return conn.execute(
            "SELECT * FROM reservoir_versions WHERE well_id=? AND formation=? "
            "ORDER BY version_no DESC LIMIT 1",
            (well_id, formation),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM reservoir_versions WHERE well_id=? ORDER BY version_no DESC LIMIT 1",
        (well_id,),
    ).fetchone()


# ---------- 外输通道 ----------

def create_channel(conn: sqlite3.Connection, actor: Actor, code: str, name: str, capacity: float) -> dict[str, Any]:
    require_departments(actor, {"pipeline", "dispatch"}, "登记外输通道")
    if capacity < 0:
        raise ValidationError("通道能力不能为负")
    cid = new_id("chn")
    conn.execute(
        "INSERT INTO export_channels(id, code, name, capacity, created_at) VALUES (?,?,?,?,?)",
        (cid, code.strip(), name.strip(), float(capacity), now()),
    )
    conn.commit()
    return {"id": cid, "code": code, "name": name, "capacity": float(capacity)}


def add_route(conn: sqlite3.Connection, actor: Actor, well_code: str, channel_code: str, share: float = 1.0) -> None:
    require_departments(actor, {"pipeline", "dispatch"}, "配置外输路由")
    if not 0 < share <= 1:
        raise ValidationError("路由占比必须在 (0,1] 区间")
    well = get_well(conn, well_code)
    channel = conn.execute("SELECT * FROM export_channels WHERE code = ?", (channel_code,)).fetchone()
    if channel is None:
        raise NotFound(f"外输通道不存在: {channel_code}")
    conn.execute(
        "INSERT INTO export_routes(id, well_id, channel_id, share) VALUES (?,?,?,?)",
        (new_id("rt"), well["id"], channel["id"], share),
    )
    conn.commit()


# ---------- 合同 ----------

def create_contract(conn: sqlite3.Connection, actor: Actor, code: str, customer: str) -> dict[str, Any]:
    require_departments(actor, {"planning", "dispatch"}, "登记供气合同")
    cid = new_id("ct")
    conn.execute(
        "INSERT INTO contracts(id, code, customer, created_at) VALUES (?,?,?,?)",
        (cid, code.strip(), customer.strip(), now()),
    )
    conn.commit()
    return {"id": cid, "code": code, "customer": customer}
