"""供气承诺与可靠供应计算。

任何可用产能都必须同时给出：
1. 适用时间（供气日落在产能版本 valid_from..valid_to 内）；
2. 置信依据（confidence + confidence_note，追溯到测试批次/解释版本/作业/例外审批）。

可用产能折减只改变尚未履行的安排：
- 维修停井、检修锁、作业窗口在当日令井不可用；
- 管网限输在窗口内压减通道上限；
这些折减在每次评估时按当日状态计算；已经冻结的历史供气决定版本不回改，
重新评估只会产生新版本，并在证据快照中说明差异。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .auth import Actor, require_contract_visibility, require_departments
from .catalog import get_well
from .errors import Conflict, NotFound, ValidationError
from .operations import channel_locked, shutdown_active, well_locked
from .util import new_id, now, require_date

INTERNAL = {"dispatch", "planning", "pipeline", "geology", "drilling", "maintenance"}


# ---------- 配置类操作 ----------

def add_restriction(
    conn: sqlite3.Connection, actor: Actor, channel_code: str, cap_rate: float,
    valid_from: str, valid_to: str, reason: str,
) -> dict[str, Any]:
    """登记管网限输窗口，只影响窗口内尚未履行的安排。"""
    require_departments(actor, {"pipeline", "dispatch"}, "登记管网限输")
    require_date(valid_from, "valid_from")
    require_date(valid_to, "valid_to")
    if valid_to < valid_from:
        raise ValidationError("限输截止日不得早于开始日")
    if cap_rate < 0:
        raise ValidationError("限输上限不能为负")
    ch = conn.execute("SELECT * FROM export_channels WHERE code=?", (channel_code,)).fetchone()
    if ch is None:
        raise NotFound(f"外输通道不存在: {channel_code}")
    rid = new_id("cr")
    conn.execute(
        "INSERT INTO channel_restrictions(id, channel_id, reason, cap_rate, valid_from, valid_to, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (rid, ch["id"], reason, float(cap_rate), valid_from, valid_to, actor.id, now()),
    )
    conn.commit()
    return {"id": rid, "channel_code": channel_code, "cap_rate": float(cap_rate),
            "valid_from": valid_from, "valid_to": valid_to}


def create_commitment(
    conn: sqlite3.Connection, actor: Actor, contract_code: str, code: str,
    daily_volume: float, valid_from: str, valid_to: str,
) -> dict[str, Any]:
    require_departments(actor, {"planning", "dispatch"}, "登记供气承诺")
    require_date(valid_from, "valid_from")
    require_date(valid_to, "valid_to")
    if daily_volume < 0:
        raise ValidationError("承诺日量不能为负")
    contract = conn.execute("SELECT * FROM contracts WHERE code=?", (contract_code,)).fetchone()
    if contract is None:
        raise NotFound(f"合同不存在: {contract_code}")
    if conn.execute("SELECT 1 FROM commitments WHERE code=?", (code,)).fetchone():
        raise Conflict(f"承诺编号已存在: {code}")
    cid = new_id("cm")
    conn.execute(
        "INSERT INTO commitments(id, contract_id, code, daily_volume, valid_from, valid_to, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (cid, contract["id"], code, float(daily_volume), valid_from, valid_to, actor.id, now()),
    )
    conn.commit()
    return {"id": cid, "code": code, "contract_id": contract["id"],
            "daily_volume": float(daily_volume)}


def add_commitment_source(
    conn: sqlite3.Connection, actor: Actor, commitment_code: str, well_code: str,
    share: float = 1.0,
) -> None:
    """把井纳入承诺的供应组合。"""
    require_departments(actor, {"planning", "dispatch"}, "配置承诺供应组合")
    if not 0 < share <= 1:
        raise ValidationError("供应占比必须在 (0,1] 区间")
    commitment = _get_commitment_by_code(conn, commitment_code)
    well = get_well(conn, well_code)
    conn.execute(
        "INSERT OR IGNORE INTO commitment_sources(id, commitment_id, well_id, share) VALUES (?,?,?,?)",
        (new_id("cs"), commitment["id"], well["id"], share),
    )
    conn.commit()


def add_nomination(
    conn: sqlite3.Connection, actor: Actor, commitment_code: str, gas_date: str, volume: float,
) -> None:
    """逐日供气提名（需求量）；重复提名按当日唯一约束拒绝，更改走撤销后重报。"""
    require_departments(actor, {"planning", "dispatch"}, "申报供气提名")
    require_date(gas_date, "gas_date")
    if volume < 0:
        raise ValidationError("提名量不能为负")
    commitment = _get_commitment_by_code(conn, commitment_code)
    conn.execute(
        "INSERT INTO nominations(id, commitment_id, gas_date, volume, created_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id("nom"), commitment["id"], gas_date, float(volume), actor.id, now()),
    )
    conn.commit()


# ---------- 产能计算 ----------

def _well_contributions(conn: sqlite3.Connection, well_id: str, gas_date: str) -> list[dict[str, Any]]:
    """汇总一口井在某供气日的全部有效产能版本，并标明口径与置信依据。

    口径不重叠原则（防止重复承诺）：
    - 例外(exception)在其窗口内替代测试口径基数；
    - 复产/措施(workover)是相对基数的增量，叠加计算；
    - 测试谱系(testing/trial/stable)与基线(baseline)取其一为基数。
    """
    rows = conn.execute(
        "SELECT v.*, c.origin_kind AS origin_kind, c.origin_id AS origin_id, "
        "c.reservoir_id AS reservoir_id "
        "FROM capacity_claim_versions v JOIN capacity_claims c ON c.id=v.claim_id "
        "WHERE c.well_id=? AND v.valid_from <= ? AND (v.valid_to IS NULL OR v.valid_to >= ?)",
        (well_id, gas_date, gas_date),
    ).fetchall()
    versions = [dict(r) for r in rows]
    exceptions = [v for v in versions if v["basis_kind"] == "exception"]
    workovers = [v for v in versions if v["basis_kind"] == "workover"]
    test_bases = [v for v in versions if v["basis_kind"] in ("testing", "trial", "stable")]
    baselines = [v for v in versions if v["basis_kind"] == "baseline"]

    picked: list[dict[str, Any]] = []
    if exceptions:
        # 例外窗口内以例外替代基数（同窗口多条例外取最新版本）
        picked.append(_tag(exceptions[-1], "exception_override"))
    elif test_bases:
        # 有经复核的测试/试采/稳产口径时以其为基数，基线不再叠加（防止老井复测重复计产）
        picked.append(_tag(test_bases[-1], "base"))
    else:
        picked.extend(_tag(v, "base") for v in baselines)
    for w in workovers:
        picked.append(_tag(w, "incremental_gain"))
    return picked


def _tag(version: dict[str, Any], role: str) -> dict[str, Any]:
    out = dict(version)
    out["contribution_role"] = role
    return out


def _channel_cap(conn: sqlite3.Connection, channel_id: str, gas_date: str) -> tuple[float, dict[str, Any]]:
    """通道当日有效上限：基础能力与限输窗口取小；通道检修锁令其为 0。"""
    ch = conn.execute("SELECT * FROM export_channels WHERE id=?", (channel_id,)).fetchone()
    cap = float(ch["capacity"])
    note: dict[str, Any] = {"base_capacity": cap}
    lock = channel_locked(conn, channel_id, gas_date)
    if lock is not None:
        note["locked"] = {"id": lock["id"], "reason": lock["reason"],
                          "by": lock["created_by"]}
        return 0.0, note
    restriction = conn.execute(
        "SELECT * FROM channel_restrictions WHERE channel_id=? AND valid_from <= ? AND valid_to >= ? "
        "ORDER BY cap_rate ASC LIMIT 1",
        (channel_id, gas_date, gas_date),
    ).fetchone()
    if restriction is not None:
        cap = min(cap, float(restriction["cap_rate"]))
        note["restriction"] = {"id": restriction["id"], "cap_rate": restriction["cap_rate"],
                               "reason": restriction["reason"], "by": restriction["created_by"]}
    note["effective_capacity"] = cap
    return cap, note


def evaluate_commitment(
    conn: sqlite3.Connection, actor: Actor, commitment_code: str, gas_date: str,
    reason: str = "例行评估",
) -> dict[str, Any]:
    """评估某承诺在某供气日的可靠供应，冻结一个供气决定版本（只追加）。"""
    require_departments(actor, {"dispatch", "planning"}, "评估供气承诺")
    require_date(gas_date, "gas_date")
    commitment = _get_commitment_by_code(conn, commitment_code)
    contract = conn.execute(
        "SELECT * FROM contracts WHERE id=?", (commitment["contract_id"],)
    ).fetchone()

    # 1) 逐井求井侧可用产能（停井/检修锁折减）
    well_evidence: list[dict[str, Any]] = []
    sources = conn.execute(
        "SELECT s.*, w.code AS well_code, w.name AS well_name "
        "FROM commitment_sources s JOIN wells w ON w.id=s.well_id "
        "WHERE s.commitment_id=?",
        (commitment["id"],),
    ).fetchall()

    # channel -> 需求明细
    channel_demand: dict[str, dict[str, Any]] = {}

    for src in sources:
        well_id = src["well_id"]
        share = float(src["share"])
        block_reason: dict[str, Any] | None = None
        ev = shutdown_active(conn, well_id, gas_date)
        if ev is not None:
            block_reason = {"kind": "work_window", "event_id": ev["id"],
                            "event_kind": ev["kind"], "from": ev["effective_from"],
                            "to": ev["effective_to"], "by": ev["recorded_by"],
                            "note": ev["note"]}
        else:
            lk = well_locked(conn, well_id, gas_date)
            if lk is not None:
                block_reason = {"kind": "maintenance_lock", "lock_id": lk["id"],
                                "reason": lk["reason"], "from": lk["lock_from"],
                                "to": lk["lock_to"], "by": lk["created_by"]}

        contributions: list[dict[str, Any]] = []
        if block_reason is None:
            contributions = _well_contributions(conn, well_id, gas_date)

        # 该井在承诺组合内的供应量 = 各贡献 * 组合占比
        well_firm = 0.0
        well_cond = 0.0
        for c in contributions:
            amount = float(c["rate"]) * share
            bucket = "firm" if c["confidence"] == "high" else "conditional"
            c["committed_share"] = share
            c["amount"] = round(amount, 6)
            c["bucket"] = bucket
            if bucket == "firm":
                well_firm += amount
            else:
                well_cond += amount

        # 2) 路由到通道，形成通道需求
        routes = conn.execute(
            "SELECT r.share AS route_share, c.id AS channel_id, c.code AS channel_code "
            "FROM export_routes r JOIN export_channels c ON c.id=r.channel_id WHERE r.well_id=?",
            (well_id,),
        ).fetchall()
        route_rows = [dict(r) for r in routes]
        for r in route_rows:
            d = channel_demand.setdefault(r["channel_id"],
                                          {"channel_code": r["channel_code"],
                                           "firm": 0.0, "conditional": 0.0,
                                           "items": []})
            d["firm"] += well_firm * float(r["route_share"])
            d["conditional"] += well_cond * float(r["route_share"])

        # 当日日报实际值（追溯用，可能为空）
        report_row = conn.execute(
            "SELECT v.* FROM daily_reports r JOIN daily_report_versions v ON v.report_id=r.id "
            "WHERE r.well_id=? AND r.gas_date=? AND v.version_no=r.current_version",
            (well_id, gas_date),
        ).fetchone()

        well_evidence.append({
            "well_code": src["well_code"], "well_name": src["well_name"],
            "committed_share": share,
            "status": "blocked" if block_reason else "available",
            "block_reason": block_reason,
            "contributions": [{
                "claim_version_id": c["id"], "basis": c["basis_kind"],
                "origin_kind": c["origin_kind"], "origin_id": c["origin_id"],
                "role": c["contribution_role"], "rate": c["rate"],
                "amount_for_commitment": c["amount"], "bucket": c["bucket"],
                "valid_from": c["valid_from"], "valid_to": c["valid_to"],
                "confidence": c["confidence"], "confidence_note": c["confidence_note"],
                "created_by": c["created_by"],
            } for c in contributions],
            "routes": route_rows,
            "daily_report": dict(report_row) if report_row else None,
        })

    # 3) 通道限输/检修折减（firm 优先通过，剩余给 conditional）
    channel_evidence: list[dict[str, Any]] = []
    total_firm = 0.0
    total_cond = 0.0
    for channel_id, demand in channel_demand.items():
        cap, cap_note = _channel_cap(conn, channel_id, gas_date)
        firm_in = demand["firm"]
        cond_in = demand["conditional"]
        firm_deliver = min(firm_in, cap)
        cond_deliver = min(cond_in, max(0.0, cap - firm_deliver))
        total_firm += firm_deliver
        total_cond += cond_deliver
        channel_evidence.append({
            "channel_id": channel_id, "channel_code": demand["channel_code"],
            "firm_demand": round(firm_in, 6), "conditional_demand": round(cond_in, 6),
            "firm_delivered": round(firm_deliver, 6),
            "conditional_delivered": round(cond_deliver, 6),
            "curtailed": firm_in + cond_in - firm_deliver - cond_deliver > 1e-9,
            **cap_note,
        })

    total_available = round(total_firm + total_cond, 6)
    total_firm = round(total_firm, 6)
    total_cond = round(total_cond, 6)

    nom = conn.execute(
        "SELECT volume FROM nominations WHERE commitment_id=? AND gas_date=?",
        (commitment["id"], gas_date),
    ).fetchone()
    demand_volume = float(nom["volume"]) if nom else float(commitment["daily_volume"])

    if total_firm + 1e-9 >= demand_volume:
        status = "firm"
    elif total_available + 1e-9 >= demand_volume:
        status = "conditional"
    else:
        status = "shortfall"
    gap = round(max(0.0, demand_volume - total_available), 6)

    evidence = {
        "commitment_code": commitment_code,
        "contract_code": contract["code"],
        "gas_date": gas_date,
        "status": status,
        "demand_volume": demand_volume,
        "demand_basis": "nomination" if nom else "commitment_daily_volume",
        "firm_available": total_firm,
        "conditional_available": total_cond,
        "total_available": total_available,
        "gap": gap,
        "wells": well_evidence,
        "channels": channel_evidence,
        "computed_at": now(),
    }

    decision_id, version_no = _freeze_decision(
        conn, commitment["id"], gas_date, total_available, status, reason, evidence, actor,
    )
    evidence["decision_id"] = decision_id
    evidence["version_no"] = version_no

    # 4) 缺口提醒：短欠开提醒；不再短欠时自动收口历史开放提醒
    if status == "shortfall":
        conn.execute(
            "INSERT INTO gap_alerts(id, commitment_id, gas_date, gap_volume, detail, status, created_at) "
            "VALUES (?,?,?,?,?, 'open', ?) ON CONFLICT(commitment_id, gas_date) DO UPDATE SET "
            "gap_volume=excluded.gap_volume, detail=excluded.detail",
            (new_id("gap"), commitment["id"], gas_date, gap,
             f"{commitment_code} {gas_date} 缺口 {gap} 万方/日（{reason}）", now()),
        )
    else:
        conn.execute(
            "UPDATE gap_alerts SET status='resolved', closed_by=?, closed_at=? "
            "WHERE commitment_id=? AND gas_date=? AND status='open'",
            (actor.id, now(), commitment["id"], gas_date),
        )
    conn.commit()
    return evidence


def _freeze_decision(
    conn: sqlite3.Connection, commitment_id: str, gas_date: str, rate: float,
    status: str, reason: str, evidence: dict[str, Any], actor: Actor,
) -> tuple[str, int]:
    decision = conn.execute(
        "SELECT * FROM supply_decisions WHERE commitment_id=? AND gas_date=?",
        (commitment_id, gas_date),
    ).fetchone()
    if decision is None:
        decision_id = new_id("sd")
        conn.execute(
            "INSERT INTO supply_decisions(id, commitment_id, gas_date, current_version) VALUES (?,?,?,0)",
            (decision_id, commitment_id, gas_date),
        )
        version_no = 1
    else:
        decision_id = decision["id"]
        version_no = decision["current_version"] + 1
    conn.execute(
        "INSERT INTO supply_decision_versions(id, decision_id, version_no, committed_rate, status, "
        "reason, evidence_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id("sdv"), decision_id, version_no, rate, status, reason,
         json.dumps(evidence, ensure_ascii=False, sort_keys=True), actor.id, now()),
    )
    conn.execute("UPDATE supply_decisions SET current_version=? WHERE id=?", (version_no, decision_id))
    return decision_id, version_no


# ---------- 查询：追溯 ----------

def get_decision(
    conn: sqlite3.Connection, actor: Actor, commitment_code: str, gas_date: str,
    version_no: int | None = None,
) -> dict[str, Any]:
    """读取供气决定（商业用户仅能看到其被授权合同的承诺）。"""
    require_date(gas_date, "gas_date")
    commitment = _get_commitment_by_code(conn, commitment_code)
    require_contract_visibility(conn, actor, commitment["contract_id"])
    decision = conn.execute(
        "SELECT * FROM supply_decisions WHERE commitment_id=? AND gas_date=?",
        (commitment["id"], gas_date),
    ).fetchone()
    if decision is None:
        raise NotFound(f"{commitment_code} 在 {gas_date} 尚无供气决定")
    if version_no is None:
        version_no = decision["current_version"]
    v = conn.execute(
        "SELECT * FROM supply_decision_versions WHERE decision_id=? AND version_no=?",
        (decision["id"], version_no),
    ).fetchone()
    if v is None:
        raise NotFound(f"供气决定版本不存在: v{version_no}")
    out = {
        "decision_id": decision["id"], "version_no": v["version_no"],
        "current_version": decision["current_version"],
        "committed_rate": v["committed_rate"], "status": v["status"],
        "reason": v["reason"], "created_by": v["created_by"], "created_at": v["created_at"],
        "evidence": json.loads(v["evidence_json"]),
    }
    versions = conn.execute(
        "SELECT version_no, status, committed_rate, created_by, created_at, reason "
        "FROM supply_decision_versions WHERE decision_id=? ORDER BY version_no",
        (decision["id"],),
    ).fetchall()
    out["history"] = [dict(r) for r in versions]
    return out


def trace_supply(conn: sqlite3.Connection, actor: Actor, commitment_code: str, gas_date: str) -> dict[str, Any]:
    """从某日供气数字追溯到井况、作业决定、检修、限输与责任人。"""
    decision = get_decision(conn, actor, commitment_code, gas_date)
    evidence = decision["evidence"]
    responsible: dict[str, dict[str, str]] = {}
    for well in evidence["wells"]:
        for c in well["contributions"]:
            responsible.setdefault(c["created_by"],
                                   {"actor_id": c["created_by"], "actions": []})
            responsible[c["created_by"]]["actions"].append(
                f"{well['well_code']} 产能 {c['basis']} {c['rate']} 万方/日（{c['confidence_note']}）")
        if well["block_reason"]:
            by = well["block_reason"]["by"]
            responsible.setdefault(by, {"actor_id": by, "actions": []})
            responsible[by]["actions"].append(
                f"{well['well_code']} 停阻：{well['block_reason']['kind']}（{well['block_reason'].get('reason','作业窗口')}）")
    for ch in evidence["channels"]:
        if ch.get("restriction"):
            by = ch["restriction"]["by"]
            responsible.setdefault(by, {"actor_id": by, "actions": []})
            responsible[by]["actions"].append(
                f"{ch['channel_code']} 限输至 {ch['restriction']['cap_rate']}（{ch['restriction']['reason']}）")
        if ch.get("locked"):
            by = ch["locked"]["by"]
            responsible.setdefault(by, {"actor_id": by, "actions": []})
            responsible[by]["actions"].append(
                f"{ch['channel_code']} 检修锁定（{ch['locked']['reason']}）")
    # 补充责任人姓名
    for item in responsible.values():
        row = conn.execute("SELECT name, department FROM actors WHERE id=?", (item["actor_id"],)).fetchone()
        if row:
            item["name"] = row["name"]
            item["department"] = row["department"]
    decision["responsible_parties"] = list(responsible.values())
    return decision


# ---------- 工作台 ----------

def open_gap_alerts(conn: sqlite3.Connection, actor: Actor) -> list[dict[str, Any]]:
    """开放的承诺缺口提醒，跨重启保留，直至解决/忽略。"""
    if actor.is_commercial:
        # 商业用户只看自己合同的缺口
        rows = conn.execute(
            "SELECT g.*, c.code AS commitment_code FROM gap_alerts g "
            "JOIN commitments c ON c.id=g.commitment_id "
            "JOIN contract_audience a ON a.contract_id=c.contract_id AND a.actor_id=? "
            "WHERE g.status='open' ORDER BY g.gas_date",
            (actor.id,),
        ).fetchall()
    else:
        require_departments(actor, INTERNAL, "查看缺口提醒")
        rows = conn.execute(
            "SELECT g.*, c.code AS commitment_code FROM gap_alerts g "
            "JOIN commitments c ON c.id=g.commitment_id "
            "WHERE g.status='open' ORDER BY g.gas_date"
        ).fetchall()
    return [dict(r) for r in rows]


def dismiss_gap(conn: sqlite3.Connection, actor: Actor, alert_id: str) -> None:
    require_departments(actor, {"dispatch", "planning"}, "关闭缺口提醒")
    row = conn.execute("SELECT * FROM gap_alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        raise NotFound("缺口提醒不存在")
    conn.execute(
        "UPDATE gap_alerts SET status='dismissed', closed_by=?, closed_at=? WHERE id=?",
        (actor.id, now(), alert_id),
    )
    conn.commit()


def _get_commitment_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM commitments WHERE code=?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"供气承诺不存在: {code}")
    return row
