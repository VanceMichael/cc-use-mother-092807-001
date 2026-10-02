"""测试批次与产能谱系。

- 测试批次先入 pending_review（待复核），状态持久化，重启后继续复核；
- 复核通过的测试最多建立一条产能谱系；
- 测试(testing)→试采(trial)→稳产(stable) 是同一谱系上的版本接替：
  旧版本在新版本生效前一日收口，任何供气日只有一个有效版本，绝不三笔重复累计。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .auth import Actor, require_departments
from .catalog import get_well, latest_reservoir
from .errors import Conflict, NotFound, ValidationError
from .util import new_id, now, require_date

BASIS_ORDER = ("testing", "trial", "stable")
CONVERTIBLE_WELL_STATUS = {"testing": "testing", "trial": "trial", "stable": "stable"}


def register_test(
    conn: sqlite3.Connection, actor: Actor, batch_no: str, well_code: str,
    test_date: str, flow_rate: float, aof: float | None = None,
    tubing_pressure: float | None = None, formation: str | None = None,
    receipt_no: str | None = None,
) -> dict[str, Any]:
    """登记一个测试批次（井队或地质报数）。批次创建后停在待复核。"""
    require_departments(actor, {"drilling", "geology", "dispatch"}, "登记测试批次")
    require_date(test_date, "test_date")
    if flow_rate is None or flow_rate < 0:
        raise ValidationError("测试产量必须为非负数")
    well = get_well(conn, well_code)
    if conn.execute("SELECT 1 FROM tests WHERE batch_no = ?", (batch_no,)).fetchone():
        raise Conflict(f"测试批次编号已存在: {batch_no}")
    reservoir = latest_reservoir(conn, well["id"], formation) if formation else None
    tid = new_id("tst")
    conn.execute(
        "INSERT INTO tests(id, well_id, reservoir_id, batch_no, test_date, aof, flow_rate, "
        "tubing_pressure, status, created_at) VALUES (?,?,?,?,?,?,?,?, 'pending_review', ?)",
        (tid, well["id"], reservoir["id"] if reservoir else None, batch_no, test_date,
         aof, float(flow_rate), tubing_pressure, now()),
    )
    if receipt_no:
        conn.execute(
            "UPDATE receipts SET processed_test_id = ? WHERE receipt_no = ?",
            (tid, receipt_no),
        )
    conn.commit()
    return get_test(conn, tid)


def get_test(conn: sqlite3.Connection, test_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
    if row is None:
        raise NotFound("测试批次不存在")
    return dict(row)


def find_test_by_batch(conn: sqlite3.Connection, batch_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tests WHERE batch_no = ?", (batch_no,)).fetchone()
    if row is None:
        raise NotFound(f"测试批次不存在: {batch_no}")
    return dict(row)


def pending_tests(conn: sqlite3.Connection, actor: Actor) -> list[dict[str, Any]]:
    """工作台：待复核测试（系统重启后仍在此列出）。"""
    require_departments(actor, {"dispatch", "geology", "planning"}, "查看待复核测试")
    rows = conn.execute(
        "SELECT t.*, w.code AS well_code FROM tests t JOIN wells w ON w.id = t.well_id "
        "WHERE t.status = 'pending_review' ORDER BY t.test_date"
    ).fetchall()
    return [dict(r) for r in rows]


def review_test(
    conn: sqlite3.Connection, actor: Actor, batch_no: str, approve: bool,
) -> dict[str, Any]:
    """复核测试批次。复核人不能是批次登记人本人；地质与调度可复核。"""
    require_departments(actor, {"dispatch", "geology"}, "复核测试")
    test = find_test_by_batch(conn, batch_no)
    if test["status"] != "pending_review":
        raise Conflict(f"测试批次已处置: {test['status']}")
    new_status = "reviewed" if approve else "rejected"
    conn.execute(
        "UPDATE tests SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
        (new_status, actor.id, now(), test["id"]),
    )
    conn.commit()
    return get_test(conn, test["id"])


def _get_claim_by_origin(conn: sqlite3.Connection, origin_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM capacity_claims WHERE origin_kind='test_review' AND origin_id=?",
        (origin_id,),
    ).fetchone()


def convert_to_capacity(
    conn: sqlite3.Connection, actor: Actor, batch_no: str, valid_from: str,
    confidence: str = "low", confidence_note: str = "",
) -> dict[str, Any]:
    """把已复核测试转换为 testing 口径的产能谱系（每批次仅一次）。"""
    require_departments(actor, {"dispatch", "geology", "planning"}, "确认测试产能")
    require_date(valid_from, "valid_from")
    test = find_test_by_batch(conn, batch_no)
    if test["status"] == "pending_review":
        raise Conflict("测试尚未复核，不能计入可用产能")
    if test["status"] == "rejected":
        raise Conflict("测试已被否决，不能计入可用产能")
    if _get_claim_by_origin(conn, test["id"]) is not None:
        raise Conflict("该测试批次已建立产能谱系，不得重复计入")
    # 同井同层系在生效日已有测试口径产能时，拒绝再建一条并行谱系，
    # 口径变化应通过原谱系晋级表达；不同层系方可并行计产。
    overlap = conn.execute(
        "SELECT v.id FROM capacity_claim_versions v JOIN capacity_claims c ON c.id=v.claim_id "
        "WHERE c.well_id=? AND c.origin_kind='test_review' "
        "AND COALESCE(c.reservoir_id, '') = COALESCE(?, '') "
        "AND v.basis_kind IN ('testing','trial','stable') "
        "AND v.valid_from <= ? AND (v.valid_to IS NULL OR v.valid_to >= ?) LIMIT 1",
        (test["well_id"], test["reservoir_id"], valid_from, valid_from),
    ).fetchone()
    if overlap is not None:
        raise Conflict("同井同层系在该日期已有测试口径产能，口径变化必须走晋级，不得并行重复计产")
    claim_id = new_id("cap")
    conn.execute(
        "INSERT INTO capacity_claims(id, well_id, reservoir_id, origin_kind, origin_id, "
        "current_version, created_by, created_at) VALUES (?,?,?, 'test_review', ?, 0, ?, ?)",
        (claim_id, test["well_id"], test["reservoir_id"], test["id"], actor.id, now()),
    )
    version = _add_version(
        conn, claim_id, "testing", test["flow_rate"], valid_from, None,
        confidence, confidence_note or f"测试批次 {batch_no} 复核通过", actor,
    )
    conn.execute("UPDATE tests SET status='converted' WHERE id=?", (test["id"],))
    _set_well_status(conn, test["well_id"], "testing")
    conn.commit()
    return {"claim_id": claim_id, "version": version}


def promote_capacity(
    conn: sqlite3.Connection, actor: Actor, claim_id: str, basis: str,
    valid_from: str, rate: float | None = None, confidence: str = "medium",
    confidence_note: str = "",
) -> dict[str, Any]:
    """产能口径晋级：testing→trial→stable，仅允许逐级前进。"""
    require_departments(actor, {"dispatch", "geology", "planning"}, "调整产能口径")
    if basis not in BASIS_ORDER:
        raise ValidationError(f"产能口径必须是 {BASIS_ORDER} 之一")
    require_date(valid_from, "valid_from")
    claim = conn.execute("SELECT * FROM capacity_claims WHERE id=?", (claim_id,)).fetchone()
    if claim is None:
        raise NotFound("产能谱系不存在")
    current = conn.execute(
        "SELECT * FROM capacity_claim_versions WHERE claim_id=? ORDER BY version_no DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    cur_index = BASIS_ORDER.index(current["basis_kind"]) if current["basis_kind"] in BASIS_ORDER else -1
    target_index = BASIS_ORDER.index(basis)
    if target_index != cur_index + 1:
        raise Conflict(
            f"产能口径只能逐级晋级（当前 {current['basis_kind']}，请求 {basis}）"
        )
    if valid_from < current["valid_from"]:
        raise Conflict("新口径生效日不得早于谱系起始日")
    new_rate = float(rate) if rate is not None else current["rate"]
    if new_rate < 0:
        raise ValidationError("产能不能为负")
    # 旧版本在新口径生效前一日收口，保证供气日口径唯一
    close_date = (date.fromisoformat(valid_from) - timedelta(days=1)).isoformat()
    conn.execute(
        "UPDATE capacity_claim_versions SET valid_to=? WHERE id=?",
        (close_date, current["id"]),
    )
    version = _add_version(
        conn, claim_id, basis, new_rate, valid_from, None,
        confidence, confidence_note or f"由 {current['basis_kind']} 晋级", actor,
        supersedes=current["id"],
    )
    _set_well_status(conn, claim["well_id"], basis)
    conn.commit()
    return {"claim_id": claim_id, "version": version}


def _add_version(
    conn: sqlite3.Connection, claim_id: str, basis: str, rate: float,
    valid_from: str, valid_to: str | None, confidence: str, note: str,
    actor: Actor, supersedes: str | None = None,
) -> dict[str, Any]:
    if confidence not in ("low", "medium", "high"):
        raise ValidationError("置信度必须是 low/medium/high")
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no),0) AS m FROM capacity_claim_versions WHERE claim_id=?",
        (claim_id,),
    ).fetchone()
    version_no = row["m"] + 1
    vid = new_id("cv")
    conn.execute(
        "INSERT INTO capacity_claim_versions(id, claim_id, version_no, basis_kind, rate, "
        "valid_from, valid_to, confidence, confidence_note, supersedes, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (vid, claim_id, version_no, basis, float(rate), valid_from, valid_to,
         confidence, note, supersedes, actor.id, now()),
    )
    conn.execute("UPDATE capacity_claims SET current_version=? WHERE id=?", (version_no, claim_id))
    out = dict(conn.execute("SELECT * FROM capacity_claim_versions WHERE id=?", (vid,)).fetchone())
    return out


def _set_well_status(conn: sqlite3.Connection, well_id: str, status: str) -> None:
    conn.execute("UPDATE wells SET status=? WHERE id=?", (status, well_id))


# ---------- 查询 ----------

def register_baseline(
    conn: sqlite3.Connection, actor: Actor, well_code: str, rate: float,
    valid_from: str, confidence: str = "high", confidence_note: str = "历史稳产基线",
) -> dict[str, Any]:
    """登记系统上线前已稳产老井的基线产能（每井一条基线谱系）。"""
    require_departments(actor, {"dispatch", "planning"}, "登记稳产基线")
    require_date(valid_from, "valid_from")
    if rate < 0:
        raise ValidationError("基线产能不能为负")
    well = get_well(conn, well_code)
    existing = conn.execute(
        "SELECT id FROM capacity_claims WHERE well_id=? AND origin_kind='manual_baseline'",
        (well["id"],),
    ).fetchone()
    if existing is not None:
        raise Conflict("该井已登记基线，口径变化请通过产能晋级或作业复产表达")
    claim_id = new_id("cap")
    conn.execute(
        "INSERT INTO capacity_claims(id, well_id, reservoir_id, origin_kind, origin_id, "
        "current_version, created_by, created_at) VALUES (?,?,?, 'manual_baseline', ?, 0, ?, ?)",
        (claim_id, well["id"], None, f"baseline:{well['id']}", actor.id, now()),
    )
    version = _add_version(conn, claim_id, "baseline", rate, valid_from, None,
                           confidence, confidence_note, actor)
    _set_well_status(conn, well["id"], "stable")
    conn.commit()
    return {"claim_id": claim_id, "version": version}


def effective_claim_version(
    conn: sqlite3.Connection, claim_id: str, gas_date: str,
) -> sqlite3.Row | None:
    """某谱系在某供气日适用的版本（恰好一个）。"""
    return conn.execute(
        "SELECT v.*, c.well_id, c.origin_kind, c.origin_id, c.reservoir_id "
        "FROM capacity_claim_versions v JOIN capacity_claims c ON c.id = v.claim_id "
        "WHERE v.claim_id = ? AND v.valid_from <= ? "
        "AND (v.valid_to IS NULL OR v.valid_to >= ?) ORDER BY v.version_no DESC LIMIT 1",
        (claim_id, gas_date, gas_date),
    ).fetchone()


def list_claims(conn: sqlite3.Connection, well_code: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT c.id AS claim_id, c.origin_kind, c.origin_id, c.current_version, "
        "w.code AS well_code, v.version_no, v.basis_kind, v.rate, v.valid_from, v.valid_to, "
        "v.confidence, v.confidence_note "
        "FROM capacity_claims c JOIN wells w ON w.id=c.well_id "
        "JOIN capacity_claim_versions v ON v.claim_id=c.id"
    )
    params: tuple[Any, ...] = ()
    if well_code:
        sql += " WHERE w.code = ?"
        params = (well_code,)
    sql += " ORDER BY w.code, c.id, v.version_no"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
