"""产能例外：地质人员不能批准自己提交的例外。

- 例外必须给出适用时间窗（valid_from..valid_to）与理由；
- 审批人不能是提交人本人，且必须来自调度/输气/供气计划等非地质独立岗位；
- 批准的例外在时间窗内作为 exception 口径的产能声明参与供应计算。
"""

from __future__ import annotations

import sqlite3

from .auth import Actor, get_actor, require_departments
from .catalog import get_well
from .errors import Conflict, NotFound, ValidationError
from .testing import _add_version
from .util import new_id, now, require_date

# 可提交例外的岗位
SUBMIT_DEPARTMENTS = {"geology", "drilling"}
# 可独立审批的岗位（刻意不含 geology）
REVIEW_DEPARTMENTS = {"dispatch", "pipeline", "planning"}


def submit_exception(
    conn: sqlite3.Connection, actor: Actor, well_code: str, requested_rate: float,
    valid_from: str, valid_to: str, justification: str,
) -> dict[str, str]:
    require_departments(actor, SUBMIT_DEPARTMENTS, "申请产能例外")
    require_date(valid_from, "valid_from")
    require_date(valid_to, "valid_to")
    if valid_to < valid_from:
        raise ValidationError("例外截止日不得早于开始日")
    if requested_rate <= 0:
        raise ValidationError("例外产量必须为正数")
    if not justification.strip():
        raise ValidationError("申请理由不能为空")
    well = get_well(conn, well_code)
    eid = new_id("ex")
    conn.execute(
        "INSERT INTO exception_requests(id, well_id, requested_rate, valid_from, valid_to, "
        "justification, status, submitted_by, created_at) VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
        (eid, well["id"], float(requested_rate), valid_from, valid_to, justification,
         actor.id, now()),
    )
    conn.commit()
    return {"id": eid, "status": "pending"}


def review_exception(
    conn: sqlite3.Connection, actor: Actor, exception_id: str, approve: bool,
    review_note: str = "",
) -> dict[str, str]:
    """审批产能例外：提交人本人不可审批；地质岗位无审批权。"""
    require_departments(actor, REVIEW_DEPARTMENTS, "审批产能例外")
    row = conn.execute("SELECT * FROM exception_requests WHERE id=?", (exception_id,)).fetchone()
    if row is None:
        raise NotFound("产能例外不存在")
    if row["status"] != "pending":
        raise Conflict(f"例外已处置: {row['status']}")
    if row["submitted_by"] == actor.id:
        raise Conflict("不能批准自己提交的产能例外")
    # 部门集合已排除 geology；这里保留显式断言作为职责分离的不变量
    submitter = get_actor(conn, row["submitted_by"])
    assert actor.department != "geology"
    if actor.department == submitter.department and actor.department != "dispatch":
        raise Conflict("审批岗位须与提交岗位相互独立")

    if approve:
        conn.execute(
            "UPDATE exception_requests SET status='approved', reviewed_by=?, reviewed_at=?, "
            "review_note=? WHERE id=?",
            (actor.id, now(), review_note, exception_id),
        )
        claim_id = new_id("cap")
        conn.execute(
            "INSERT INTO capacity_claims(id, well_id, reservoir_id, origin_kind, origin_id, "
            "current_version, created_by, created_at) VALUES (?,?,?, 'exception', ?, 0, ?, ?)",
            (claim_id, row["well_id"], None, exception_id, actor.id, now()),
        )
        _add_version(
            conn, claim_id, "exception", row["requested_rate"], row["valid_from"],
            row["valid_to"], "medium",
            f"产能例外 {exception_id} 经独立审批（理由：{row['justification']}）", actor,
        )
    else:
        conn.execute(
            "UPDATE exception_requests SET status='rejected', reviewed_by=?, reviewed_at=?, "
            "review_note=? WHERE id=?",
            (actor.id, now(), review_note, exception_id),
        )
    conn.commit()
    return {"id": exception_id, "status": "approved" if approve else "rejected",
            "reviewed_by": actor.id}


def pending_exceptions(conn: sqlite3.Connection, actor: Actor) -> list[dict]:
    require_departments(actor, REVIEW_DEPARTMENTS | {"geology"}, "查看产能例外")
    rows = conn.execute(
        "SELECT e.*, w.code AS well_code FROM exception_requests e JOIN wells w ON w.id=e.well_id "
        "WHERE e.status='pending' ORDER BY e.created_at"
    ).fetchall()
    return [dict(r) for r in rows]
