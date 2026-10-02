"""储层解释版本与测试批次复核。

- 储层解释按井维护只增版本号，旧解释永不覆盖；
- 测试批次提交后为 pending_review，复核人不得是提交人；
- 复核结论落盘，系统重启后待复核队列继续存在。
"""

from __future__ import annotations

import sqlite3

from .errors import ConflictError, NotFoundError, PermissionError, ValidationError
from .materials import MaterialService
from .util import json_dumps, next_id, now_iso, parse_date, positive_rate

CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})


class GeologyService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.materials = MaterialService(conn)

    # -- 储层解释版本 --------------------------------------------------------
    def submit_interpretation(
        self, actor: dict, well_id: str, formation: str, payload: dict
    ) -> dict:
        self._assert_well(well_id)
        if not isinstance(formation, str) or not formation.strip():
            raise ValidationError("层系名称不能为空")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("解释内容必须是非空 JSON 对象")
        with self.conn:
            row = self.conn.execute(
                "select coalesce(max(version),0)+1 v from reservoir_interpretations "
                "where well_id=?",
                (well_id,),
            ).fetchone()
            iid = next_id(self.conn, "RSV")
            self.conn.execute(
                "insert into reservoir_interpretations(id,well_id,version,"
                "submitted_by,submitted_at,formation,payload) "
                "values(?,?,?,?,?,?,?)",
                (
                    iid,
                    well_id,
                    row["v"],
                    actor["id"],
                    now_iso(),
                    formation.strip(),
                    json_dumps(payload),
                ),
            )
        return self.get_interpretation(iid)

    def get_interpretation(self, interpretation_id: str) -> dict:
        row = self.conn.execute(
            "select * from reservoir_interpretations where id=?",
            (interpretation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("储层解释版本不存在")
        result = dict(row)
        return result

    def list_interpretations(self, well_id: str) -> list[dict]:
        self._assert_well(well_id)
        return [
            dict(r)
            for r in self.conn.execute(
                "select * from reservoir_interpretations where well_id=? "
                "order by version desc",
                (well_id,),
            )
        ]

    def latest_interpretation(self, well_id: str) -> dict:
        row = self.conn.execute(
            "select * from reservoir_interpretations where well_id=? "
            "order by version desc limit 1",
            (well_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("该井尚无储层解释版本")
        return dict(row)

    # -- 测试批次 ------------------------------------------------------------
    def submit_test_batch(
        self,
        actor: dict,
        well_id: str,
        flow_rate: float,
        test_date: str,
        basis: str,
        confidence: str,
        interpretation_id: str | None = None,
        material_id: str | None = None,
    ) -> dict:
        self._assert_well(well_id)
        rate = positive_rate(flow_rate, "测试产量")
        day = parse_date(test_date, "测试日期")
        if not isinstance(basis, str) or not basis.strip():
            raise ValidationError("必须说明测试制度/方法等置信依据")
        if confidence not in CONFIDENCE_LEVELS:
            raise ValidationError("置信度必须是 high / medium / low")

        if interpretation_id is None:
            interpretation = self.latest_interpretation(well_id)
        else:
            interpretation = self.get_interpretation(interpretation_id)
            if interpretation["well_id"] != well_id:
                raise ValidationError("解释版本与井不匹配")
        if material_id is not None:
            self.materials.assert_usable(material_id)
            m_well = self.conn.execute(
                "select well_id from inbound_materials where id=?", (material_id,)
            ).fetchone()["well_id"]
            if m_well is not None and m_well != well_id:
                raise ValidationError("回执材料与井不匹配")

        bid = next_id(self.conn, "TST")
        with self.conn:
            self.conn.execute(
                "insert into test_batches(id,well_id,interpretation_id,material_id,"
                "submitted_by,submitted_at,flow_rate,test_date,basis,confidence,status) "
                "values(?,?,?,?,?,?,?,?,?,?, 'pending_review')",
                (
                    bid,
                    well_id,
                    interpretation["id"],
                    material_id,
                    actor["id"],
                    now_iso(),
                    rate,
                    day.isoformat(),
                    basis.strip(),
                    confidence,
                ),
            )
        return self.get_test_batch(bid)

    def review_test_batch(
        self,
        actor: dict,
        batch_id: str,
        approve: bool,
        note: str = "",
    ) -> dict:
        batch = self._get_row(batch_id)
        if batch["status"] != "pending_review":
            raise ConflictError("该测试批次已复核")
        # 职责分离：任何人不得复核自己提交的测试
        if actor["id"] == batch["submitted_by"]:
            raise PermissionError("不能审批自己提交的测试批次")
        with self.conn:
            self.conn.execute(
                "update test_batches set status=?, reviewed_by=?, reviewed_at=?, "
                "review_note=? where id=?",
                (
                    "approved" if approve else "rejected",
                    actor["id"],
                    now_iso(),
                    note or None,
                    batch_id,
                ),
            )
        return self.get_test_batch(batch_id)

    def get_test_batch(self, batch_id: str) -> dict:
        return dict(self._get_row(batch_id))

    def list_test_batches(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in {
            "pending_review", "approved", "rejected"
        }:
            raise ValidationError("测试批次状态过滤非法")
        sql = "select * from test_batches"
        params: tuple = ()
        if status:
            sql += " where status=?"
            params = (status,)
        sql += " order by submitted_at, id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    # -- 内部 ----------------------------------------------------------------
    def _assert_well(self, well_id: str) -> None:
        if self.conn.execute(
            "select 1 from wells where id=?", (well_id,)
        ).fetchone() is None:
            raise NotFoundError("井不存在")

    def _get_row(self, batch_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from test_batches where id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("测试批次不存在")
        return row
