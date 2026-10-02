"""作业方案、措施事件与检修锁。

核心规则——只改变尚未履行的安排：
- 维修停井、酸化解堵、压裂调整都以「事件 + 生效窗口」追加登记；
- 这些窗口只参与生效日当天及以后的可用产能计算；
- 已经形成的日报与供气决定绝不回改，后续变化通过新版本/更正衔接；
- 检修锁持久化，系统重启后仍可在工作台看到待解锁井/通道。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .auth import Actor, require_departments
from .catalog import get_well
from .errors import Conflict, NotFound, ValidationError
from .testing import _add_version, _set_well_status
from .util import new_id, now, require_date


# ---------- 作业方案 ----------

def create_plan(
    conn: sqlite3.Connection, actor: Actor, well_code: str, kind: str, plan_no: str,
) -> dict[str, Any]:
    require_departments(actor, {"drilling", "maintenance", "dispatch"}, "提出作业方案")
    if kind not in ("workover", "acidizing", "fracturing", "other"):
        raise ValidationError("作业类型无效")
    well = get_well(conn, well_code)
    if conn.execute("SELECT 1 FROM work_plans WHERE plan_no=?", (plan_no,)).fetchone():
        raise Conflict(f"作业方案编号已存在: {plan_no}")
    pid = new_id("wp")
    conn.execute(
        "INSERT INTO work_plans(id, well_id, plan_no, kind, status, proposed_by, created_at) "
        "VALUES (?,?,?,?, 'proposed', ?, ?)",
        (pid, well["id"], plan_no, kind, actor.id, now()),
    )
    conn.commit()
    return get_plan(conn, pid)


def get_plan(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM work_plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise NotFound("作业方案不存在")
    out = dict(row)
    out["events"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM work_plan_events WHERE plan_id=? ORDER BY effective_from, created_at",
            (plan_id,),
        ).fetchall()
    ]
    return out


def find_plan(conn: sqlite3.Connection, plan_no: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM work_plans WHERE plan_no=?", (plan_no,)).fetchone()
    if row is None:
        raise NotFound(f"作业方案不存在: {plan_no}")
    return row


def approve_plan(conn: sqlite3.Connection, actor: Actor, plan_no: str) -> dict[str, Any]:
    """审批作业方案；提出人不能审批自己的方案。"""
    require_departments(actor, {"dispatch", "maintenance", "pipeline"}, "审批作业方案")
    plan = find_plan(conn, plan_no)
    if plan["status"] != "proposed":
        raise Conflict(f"方案已处置: {plan['status']}")
    if plan["proposed_by"] == actor.id:
        raise Conflict("提出人不能审批自己提交的作业方案")
    conn.execute(
        "UPDATE work_plans SET status='approved', approved_by=?, approved_at=? WHERE id=?",
        (actor.id, now(), plan["id"]),
    )
    _append_event(conn, plan["id"], actor, "approved", today_min(now()), None, None, None,
                  "方案批准")
    conn.commit()
    return get_plan(conn, plan["id"])


def record_shutdown(
    conn: sqlite3.Connection, actor: Actor, plan_no: str, effective_from: str,
    effective_to: str | None = None, note: str = "",
) -> dict[str, Any]:
    """登记停井窗口（维修/酸化/压裂期间）。窗口只影响生效日起的未来安排。"""
    require_departments(actor, {"drilling", "maintenance", "dispatch"}, "登记停井")
    require_date(effective_from, "effective_from")
    if effective_to is not None:
        require_date(effective_to, "effective_to")
        if effective_to < effective_from:
            raise ValidationError("停井截止日不得早于开始日")
    plan = find_plan(conn, plan_no)
    if plan["status"] == "cancelled":
        raise Conflict("方案已取消")
    kind = {"workover": "shutdown", "acidizing": "acidizing",
            "fracturing": "fracturing", "other": "shutdown"}[plan["kind"]]
    event = _append_event(conn, plan["id"], actor, kind, effective_from, effective_to,
                          None, None, note)
    _set_well_status(conn, plan["well_id"], "suspended")
    conn.commit()
    return event


def record_revival(
    conn: sqlite3.Connection, actor: Actor, plan_no: str, effective_from: str,
    expected_gain: float, confidence: str = "medium", note: str = "",
) -> dict[str, Any]:
    """登记复产/措施见效：关闭未结束的停井窗口，并建立一条 workover 口径产能谱系。

    复产只影响 effective_from 起的安排；同一复产事件最多建立一条谱系。
    """
    require_departments(actor, {"drilling", "maintenance", "dispatch", "geology"}, "登记复产")
    require_date(effective_from, "effective_from")
    if expected_gain < 0:
        raise ValidationError("复产增量不能为负")
    if confidence not in ("low", "medium", "high"):
        raise ValidationError("置信度必须是 low/medium/high")
    plan = find_plan(conn, plan_no)
    # 收口该方案仍覆盖复产日的停井窗口：复产日当天井已可用，窗口截止到前一日
    close_date = (date.fromisoformat(effective_from) - timedelta(days=1)).isoformat()
    open_events = conn.execute(
        "SELECT id, effective_from FROM work_plan_events WHERE plan_id=? "
        "AND kind IN ('shutdown','acidizing','fracturing') "
        "AND effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)",
        (plan["id"], close_date, effective_from),
    ).fetchall()
    for ev in open_events:
        conn.execute("UPDATE work_plan_events SET effective_to=? WHERE id=?",
                     (close_date, ev["id"]))
    event = _append_event(conn, plan["id"], actor, "revival", effective_from, None,
                          float(expected_gain), confidence, note)
    conn.execute("UPDATE work_plans SET status='executed' WHERE id=?", (plan["id"],))

    # 复产产能谱系（每事件唯一）
    claim = conn.execute(
        "SELECT id FROM capacity_claims WHERE origin_kind='workover_revival' AND origin_id=?",
        (event["id"],),
    ).fetchone()
    if claim is None:
        claim_id = new_id("cap")
        conn.execute(
            "INSERT INTO capacity_claims(id, well_id, reservoir_id, origin_kind, origin_id, "
            "current_version, created_by, created_at) "
            "VALUES (?,?,?, 'workover_revival', ?, 0, ?, ?)",
            (claim_id, plan["well_id"], None, event["id"], actor.id, now()),
        )
        _add_version(
            conn, claim_id, "workover", expected_gain, effective_from, None,
            confidence, note or f"方案 {plan['plan_no']} 复产/措施见效", actor,
        )
    conn.commit()
    return event


def _append_event(
    conn: sqlite3.Connection, plan_id: str, actor: Actor, kind: str,
    effective_from: str, effective_to: str | None, expected_gain: float | None,
    confidence: str | None, note: str,
) -> dict[str, Any]:
    eid = new_id("wpe")
    conn.execute(
        "INSERT INTO work_plan_events(id, plan_id, kind, effective_from, effective_to, "
        "expected_gain, confidence, note, recorded_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (eid, plan_id, kind, effective_from, effective_to, expected_gain, confidence,
         note, actor.id, now()),
    )
    return dict(conn.execute("SELECT * FROM work_plan_events WHERE id=?", (eid,)).fetchone())


def shutdown_active(conn: sqlite3.Connection, well_id: str, gas_date: str) -> sqlite3.Row | None:
    """该井在指定供气日是否处于停井/措施窗口内。"""
    return conn.execute(
        "SELECT e.* FROM work_plan_events e JOIN work_plans p ON p.id=e.plan_id "
        "WHERE p.well_id=? AND e.kind IN ('shutdown','acidizing','fracturing') "
        "AND e.effective_from <= ? AND (e.effective_to IS NULL OR e.effective_to >= ?) "
        "LIMIT 1",
        (well_id, gas_date, gas_date),
    ).fetchone()


# ---------- 检修锁 ----------

def create_lock(
    conn: sqlite3.Connection, actor: Actor, lock_from: str, lock_to: str, reason: str,
    well_code: str | None = None, channel_code: str | None = None,
) -> dict[str, Any]:
    require_departments(actor, {"maintenance", "dispatch", "pipeline"}, "登记检修锁")
    require_date(lock_from, "lock_from")
    require_date(lock_to, "lock_to")
    if lock_to < lock_from:
        raise ValidationError("检修窗口截止日不得早于开始日")
    well_id = None
    channel_id = None
    if well_code:
        well_id = get_well(conn, well_code)["id"]
    if channel_code:
        row = conn.execute("SELECT id FROM export_channels WHERE code=?", (channel_code,)).fetchone()
        if row is None:
            raise NotFound(f"外输通道不存在: {channel_code}")
        channel_id = row["id"]
    if not well_id and not channel_id:
        raise ValidationError("检修锁必须约束井或外输通道之一")
    lid = new_id("lck")
    conn.execute(
        "INSERT INTO maintenance_locks(id, well_id, channel_id, reason, lock_from, lock_to, "
        "status, created_by, created_at) VALUES (?,?,?,?,?,?, 'locked', ?, ?)",
        (lid, well_id, channel_id, reason, lock_from, lock_to, actor.id, now()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM maintenance_locks WHERE id=?", (lid,)).fetchone())


def unlock(conn: sqlite3.Connection, actor: Actor, lock_id: str) -> dict[str, Any]:
    """解除检修锁——仅维护部门。重启后待解锁记录仍在此。"""
    require_departments(actor, {"maintenance"}, "解除检修锁")
    row = conn.execute("SELECT * FROM maintenance_locks WHERE id=?", (lock_id,)).fetchone()
    if row is None:
        raise NotFound("检修锁不存在")
    if row["status"] == "unlocked":
        raise Conflict("检修锁已解除")
    conn.execute(
        "UPDATE maintenance_locks SET status='unlocked', unlocked_by=?, unlocked_at=? WHERE id=?",
        (actor.id, now(), lock_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM maintenance_locks WHERE id=?", (lock_id,)).fetchone())


def locked_locks(conn: sqlite3.Connection, actor: Actor) -> list[dict[str, Any]]:
    """工作台：未解除的检修锁（跨重启保留）。"""
    require_departments(actor, {"maintenance", "dispatch", "pipeline", "drilling", "geology", "planning"},
                        "查看检修锁")
    rows = conn.execute(
        "SELECT l.*, w.code AS well_code, c.code AS channel_code "
        "FROM maintenance_locks l LEFT JOIN wells w ON w.id=l.well_id "
        "LEFT JOIN export_channels c ON c.id=l.channel_id "
        "WHERE l.status='locked' ORDER BY l.lock_from"
    ).fetchall()
    return [dict(r) for r in rows]


def well_locked(conn: sqlite3.Connection, well_id: str, gas_date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM maintenance_locks WHERE well_id=? AND status='locked' "
        "AND lock_from <= ? AND lock_to >= ? LIMIT 1",
        (well_id, gas_date, gas_date),
    ).fetchone()


def channel_locked(conn: sqlite3.Connection, channel_id: str, gas_date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM maintenance_locks WHERE channel_id=? AND status='locked' "
        "AND lock_from <= ? AND lock_to >= ? LIMIT 1",
        (channel_id, gas_date, gas_date),
    ).fetchone()


def today_min(_timestamp: str) -> str:
    """从 ISO 时间戳取日期部分。"""
    return _timestamp[:10]
