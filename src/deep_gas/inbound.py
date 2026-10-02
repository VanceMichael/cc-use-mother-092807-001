"""来件入口：测井/生产回执登记。

规则：
- 回执编号唯一：同一编号重复到达直接识别为重复件，绝不再次累计；
- 编号相同但内容（数值）不同：不覆盖、不累计，先入隔离区人工裁决。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .auth import Actor, reject_commercial
from .errors import Conflict, NotFound, Quarantined
from .util import now


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ingest(
    conn: sqlite3.Connection, actor: Actor, receipt_no: str,
    source_kind: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """登记一份来件，返回处置结果。

    outcome:
      accepted   —— 首次登记，进入台账，等待并入业务处理；
      duplicate  —— 编号与内容均一致，重复件，直接忽略，不产生任何累计；
      quarantined —— 编号相同但数值不同，已隔离（同时抛出 Quarantined）。
    """
    reject_commercial(actor, "登记来件回执")
    if not receipt_no.strip():
        raise Conflict("回执编号不能为空")
    phash = _payload_hash(payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    existing = conn.execute(
        "SELECT payload_json FROM receipts WHERE receipt_no = ?", (receipt_no,)
    ).fetchone()
    if existing is not None:
        if _payload_hash(json.loads(existing["payload_json"])) == phash:
            return {"outcome": "duplicate", "receipt_no": receipt_no,
                    "message": "回执已登记，重复到达不再累计"}
        _quarantine(conn, actor, receipt_no, source_kind, payload_json, phash,
                    "回执编号已存在且数值不同")
        conn.commit()
        raise Quarantined(f"回执 {receipt_no} 与已登记内容数值不同，已隔离待裁决")

    # 该编号此前可能已被隔离过：与隔离件逐份比对
    q_rows = conn.execute(
        "SELECT payload_hash, status FROM quarantined_materials WHERE receipt_no = ?",
        (receipt_no,),
    ).fetchall()
    for q in q_rows:
        if q["payload_hash"] == phash:
            return {"outcome": "duplicate", "receipt_no": receipt_no,
                    "message": "与隔离区中的材料完全相同，仍待人工裁决，不重复登记"}
    if q_rows:
        _quarantine(conn, actor, receipt_no, source_kind, payload_json, phash,
                    "同一回执编号出现第三种内容")
        conn.commit()
        raise Quarantined(f"回执 {receipt_no} 再次出现不同数值，已追加隔离")

    rid = "rcp_" + hashlib.sha1((receipt_no + phash).encode()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO receipts(id, receipt_no, source_kind, payload_json, received_at) "
        "VALUES (?,?,?,?,?)",
        (rid, receipt_no.strip(), source_kind, payload_json, now()),
    )
    conn.commit()
    return {"outcome": "accepted", "receipt_no": receipt_no, "id": rid}


def _quarantine(
    conn: sqlite3.Connection, actor: Actor, receipt_no: str, source_kind: str,
    payload_json: str, phash: str, reason: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO quarantined_materials"
        "(id, receipt_no, source_kind, payload_json, payload_hash, reason, received_by, received_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("q_" + phash[:16], receipt_no.strip(), source_kind, payload_json, phash,
         reason, actor.id, now()),
    )


def resolve_quarantine(
    conn: sqlite3.Connection, actor: Actor, quarantine_id: str, decision: str,
) -> dict[str, Any]:
    """裁决隔离材料：accept 采用该版本入台账（原台账版本仍保留并由后续更正衔接），
    discard 丢弃。只有调度可以裁决。"""
    from .auth import require_departments
    require_departments(actor, {"dispatch"}, "裁决隔离材料")
    row = conn.execute(
        "SELECT * FROM quarantined_materials WHERE id = ?", (quarantine_id,)
    ).fetchone()
    if row is None:
        raise NotFound("隔离记录不存在")
    if row["status"] != "quarantined":
        raise Conflict(f"隔离材料已裁决: {row['status']}")
    if decision == "discard":
        conn.execute(
            "UPDATE quarantined_materials SET status='discarded' WHERE id=?",
            (quarantine_id,),
        )
    elif decision == "accept":
        exists = conn.execute(
            "SELECT 1 FROM receipts WHERE receipt_no = ?", (row["receipt_no"],)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO receipts(id, receipt_no, source_kind, payload_json, received_at) "
                "VALUES (?,?,?,?,?)",
                ("rcp_" + row["payload_hash"][:16], row["receipt_no"],
                 row["source_kind"], row["payload_json"], now()),
            )
        conn.execute(
            "UPDATE quarantined_materials SET status='accepted' WHERE id=?",
            (quarantine_id,),
        )
    else:
        raise Conflict("裁决结果只能是 accept 或 discard")
    conn.commit()
    return {"id": quarantine_id, "decision": decision}


def list_quarantine(conn: sqlite3.Connection, actor: Actor) -> list[dict[str, Any]]:
    from .auth import require_departments
    require_departments(actor, {"dispatch", "geology", "maintenance", "pipeline", "drilling", "planning"}, "查看隔离区")
    rows = conn.execute(
        "SELECT * FROM quarantined_materials WHERE status='quarantined' ORDER BY received_at"
    ).fetchall()
    return [dict(r) for r in rows]
