"""单井日产日报。

- 日报提交即定稿，不提供删除或覆盖；
- 数值更正必须显式发起，生成 version+1 并通过 corrects_id 衔接旧版；
- 可附生产回执材料，但材料必须是已接收/已放行状态（隔离件不可用）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .errors import ConflictError, NotFoundError, ValidationError
from .materials import MaterialService
from .util import next_id, now_iso, parse_date, positive_rate


class ReportService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.materials = MaterialService(conn)
        # 由 ServiceHub 注入：日报定稿后核对安排履行状态
        self.after_finalize = lambda actor, well_id, gas_date: None

    def submit_report(
        self,
        actor: dict,
        well_id: str,
        gas_date: str,
        rate: float,
        material_id: str | None = None,
    ) -> dict:
        self._assert_well(well_id)
        day = parse_date(gas_date, "生产日期")
        value = positive_rate(rate, "日产")
        if material_id is not None:
            self.materials.assert_usable(material_id)
        existing = self._latest_row(well_id, day)
        if existing is not None:
            raise ConflictError(
                "该井当日已有定稿日报；数值变化必须走日报更正，不得重复提交",
                details={"existing_report": existing["id"], "version": existing["version"]},
            )
        rid = next_id(self.conn, "RPT")
        with self.conn:
            self.conn.execute(
                "insert into daily_reports(id,well_id,gas_date,version,rate,status,"
                "submitted_by,submitted_at,basis_material_id) "
                "values(?,?,?,1,?, 'finalized',?,?,?)",
                (rid, well_id, day.isoformat(), value, actor["id"], now_iso(),
                 material_id),
            )
        self.after_finalize(actor, well_id, day.isoformat())
        return self.get_report(rid)

    def correct_report(
        self,
        actor: dict,
        well_id: str,
        gas_date: str,
        rate: float,
        note: str,
        material_id: str | None = None,
    ) -> dict:
        """以新版本更正既有日报；旧版保留并标记 corrected。"""
        day = parse_date(gas_date, "生产日期")
        value = positive_rate(rate, "日产")
        if not isinstance(note, str) or not note.strip():
            raise ValidationError("日报更正必须说明原因")
        if material_id is not None:
            self.materials.assert_usable(material_id)
        latest = self._latest_row(well_id, day)
        if latest is None:
            raise NotFoundError("该井当日尚无日报，不能更正；请先提交日报")
        if abs(latest["rate"] - value) < 1e-9 and latest["basis_material_id"] == material_id:
            raise ConflictError("更正数值与当前版本一致，无需更正")
        rid = next_id(self.conn, "RPT")
        with self.conn:
            self.conn.execute(
                "insert into daily_reports(id,well_id,gas_date,version,rate,status,"
                "submitted_by,submitted_at,basis_material_id,corrects_id,correction_note) "
                "values(?,?,?,?,?, 'finalized',?,?,?,?,?)",
                (rid, well_id, day.isoformat(), latest["version"] + 1, value,
                 actor["id"], now_iso(), material_id, latest["id"], note.strip()),
            )
            self.conn.execute(
                "update daily_reports set status='corrected' where id=?",
                (latest["id"],),
            )
        self.after_finalize(actor, well_id, day.isoformat())
        return self.get_report(rid)

    def get_report(self, report_id: str) -> dict:
        row = self.conn.execute(
            "select * from daily_reports where id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("日报不存在")
        return dict(row)

    def latest_report(self, well_id: str, gas_date: str) -> dict | None:
        row = self._latest_row(well_id, parse_date(gas_date, "生产日期"))
        return dict(row) if row else None

    def report_chain(self, well_id: str, gas_date: str) -> list[dict]:
        """返回某井某日从初版到最新的完整更正链。"""
        rows = self.conn.execute(
            "select * from daily_reports where well_id=? and gas_date=? "
            "order by version",
            (well_id, gas_date),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 内部 -----------------------------------------------------------------
    def _assert_well(self, well_id: str) -> None:
        if self.conn.execute(
            "select 1 from wells where id=?", (well_id,)
        ).fetchone() is None:
            raise NotFoundError("井不存在")

    def _latest_row(self, well_id: str, day: date) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from daily_reports where well_id=? and gas_date=? "
            "order by version desc limit 1",
            (well_id, day.isoformat()),
        ).fetchone()
