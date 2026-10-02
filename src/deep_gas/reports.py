"""井日报：只追加版本，更正必须衔接前驱。

- 同一井同一供气日的首份日报为 v1；
- 后续到达的数字只能作为 v2、v3… 更正版本，记录更正人并引用前驱；
- 任何历史版本都不被覆盖，供气决定与追溯始终能还原当日依据。
"""

from __future__ import annotations

import sqlite3

from .auth import Actor, require_departments
from .catalog import get_well
from .errors import Conflict, NotFound, ValidationError
from .util import new_id, now, require_date


def record_daily(
    conn: sqlite3.Connection, actor: Actor, well_code: str, gas_date: str,
    actual_rate: float, note: str = "",
) -> dict[str, dict]:
    """登记日报；若该供气日已有版本，则生成衔接前驱的更正版本。"""
    require_departments(actor, {"drilling", "dispatch", "geology", "maintenance", "pipeline", "planning"},
                        "登记日报")
    require_date(gas_date, "gas_date")
    if actual_rate < 0:
        raise ValidationError("日产量不能为负")
    well = get_well(conn, well_code)

    report = conn.execute(
        "SELECT * FROM daily_reports WHERE well_id=? AND gas_date=?",
        (well["id"], gas_date),
    ).fetchone()
    if report is None:
        report_id = new_id("rpt")
        conn.execute(
            "INSERT INTO daily_reports(id, well_id, gas_date, current_version) VALUES (?,?,?,0)",
            (report_id, well["id"], gas_date),
        )
        prev_id = None
    else:
        report_id = report["id"]
        if report["current_version"] >= 1:
            prev = conn.execute(
                "SELECT id FROM daily_report_versions WHERE report_id=? ORDER BY version_no DESC LIMIT 1",
                (report_id,),
            ).fetchone()
            prev_id = prev["id"]
        else:
            prev_id = None

    version_no = report["current_version"] + 1 if report else 1
    vid = new_id("rv")
    conn.execute(
        "INSERT INTO daily_report_versions(id, report_id, version_no, actual_rate, note, "
        "correction_of, recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (vid, report_id, version_no, float(actual_rate), note, prev_id, actor.id, now()),
    )
    conn.execute("UPDATE daily_reports SET current_version=? WHERE id=?", (version_no, report_id))
    conn.commit()
    return get_report(conn, report_id)


def get_report(conn: sqlite3.Connection, report_id: str) -> dict[str, dict]:
    report = conn.execute("SELECT * FROM daily_reports WHERE id=?", (report_id,)).fetchone()
    if report is None:
        raise NotFound("日报不存在")
    versions = [
        dict(r) for r in conn.execute(
            "SELECT * FROM daily_report_versions WHERE report_id=? ORDER BY version_no",
            (report_id,),
        ).fetchall()
    ]
    return {"report": dict(report), "versions": versions,
            "current": versions[-1] if versions else None}


def get_well_report(conn: sqlite3.Connection, well_code: str, gas_date: str) -> dict[str, dict] | None:
    well = get_well(conn, well_code)
    report = conn.execute(
        "SELECT * FROM daily_reports WHERE well_id=? AND gas_date=?",
        (well["id"], gas_date),
    ).fetchone()
    return get_report(conn, report["id"]) if report else None


def day_actuals(conn: sqlite3.Connection, gas_date: str) -> dict[str, float]:
    """某供气日各井最新版日报实际产量（供追溯与事后对比）。"""
    rows = conn.execute(
        "SELECT r.well_id AS well_id, v.actual_rate AS actual_rate "
        "FROM daily_reports r JOIN daily_report_versions v ON v.report_id=r.id "
        "WHERE r.gas_date=? AND v.version_no=r.current_version",
        (gas_date,),
    ).fetchall()
    return {row["well_id"]: row["actual_rate"] for row in rows}
