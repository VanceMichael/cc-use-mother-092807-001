"""产能声明：可靠产能的唯一登记口。

不变量：
1. 任何可用产能都必须声明适用时间窗（valid_from/valid_to）与置信依据
   （confidence + basis + 来源）；
2. 同一井在同一时刻至多有一条 active 产能，测试→试采→稳产是口径更替
   （旧声明 superseded），不是新增产量；
3. 同一来源（测试批次/例外/被更替声明）只能生成一条产能；
4. 产能例外由地质提交、调度审批，审批人不得是提交人。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .errors import ConflictError, NotFoundError, PermissionError, ValidationError
from .geology import CONFIDENCE_LEVELS
from .util import next_id, now_iso, parse_date, positive_rate

STAGES = ("tested", "trial", "stable", "exception")
PROMOTION = {"tested": "trial", "trial": "stable"}
ACTIVE_OFFER_STATUSES = ("active",)


class CapacityService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # 由 ServiceHub 注入：产能口径变化 -> 重算未来供气安排
        self.supply_changed = lambda actor, reason: None

    # -- 由已复核测试发布产能 -------------------------------------------------
    def publish_from_test(
        self,
        actor: dict,
        batch_id: str,
        valid_from: str,
        valid_to: str,
        rate: float | None = None,
    ) -> dict:
        batch = self.conn.execute(
            "select * from test_batches where id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFoundError("测试批次不存在")
        if batch["status"] != "approved":
            raise ConflictError("只有复核通过的测试批次才能登记产能")
        start, end = self._validate_window(valid_from, valid_to)
        offer_rate = batch["flow_rate"] if rate is None else positive_rate(rate)
        self._assert_no_active(batch["well_id"])
        self._assert_source_unused("test_batch", batch_id)

        oid = next_id(self.conn, "CAP")
        with self.conn:
            self.conn.execute(
                "insert into capacity_offers(id,well_id,stage,valid_from,valid_to,"
                "rate,confidence,basis,source_type,source_id,created_by,created_at,"
                "status) values(?,?,?,?,?,?,?,?,?,?,?,?, 'active')",
                (
                    oid,
                    batch["well_id"],
                    "tested",
                    start.isoformat(),
                    end.isoformat(),
                    offer_rate,
                    batch["confidence"],
                    f"经复核测试 {batch_id}：{batch['basis']}",
                    "test_batch",
                    batch_id,
                    actor["id"],
                    now_iso(),
                ),
            )
            self.conn.execute(
                "update wells set status='testing' where id=?", (batch["well_id"],)
            )
        self.supply_changed(actor, f"测试 {batch_id} 登记产能 {oid}")
        return self.get_offer(oid)

    # -- 口径转正：测试→试采→稳产（更替，不叠加）------------------------------
    def promote(
        self,
        actor: dict,
        offer_id: str,
        valid_from: str,
        valid_to: str,
        rate: float,
        confidence: str,
        basis: str,
    ) -> dict:
        old = self._get_offer_row(offer_id)
        if old["status"] != "active":
            raise ConflictError("只有生效中的产能才能转正")
        next_stage = PROMOTION.get(old["stage"])
        if next_stage is None:
            raise ConflictError(
                "稳产产能为最终口径；例外产能不能转正，请走正常测试流程"
                if old["stage"] != "stable"
                else "稳产产能已是最终口径"
            )
        if confidence not in CONFIDENCE_LEVELS:
            raise ValidationError("置信度必须是 high / medium / low")
        if not isinstance(basis, str) or not basis.strip():
            raise ValidationError("转正必须说明新的置信依据（试采/稳产表现）")
        start, end = self._validate_window(valid_from, valid_to)
        new_rate = positive_rate(rate, "产能")
        self._assert_source_unused("promotion", offer_id)

        nid = next_id(self.conn, "CAP")
        well_status = "trial" if next_stage == "trial" else "stable"
        with self.conn:
            self.conn.execute(
                "insert into capacity_offers(id,well_id,stage,valid_from,valid_to,"
                "rate,confidence,basis,source_type,source_id,created_by,created_at,"
                "status) values(?,?,?,?,?,?,?,?,?,?,?,?, 'active')",
                (
                    nid,
                    old["well_id"],
                    next_stage,
                    start.isoformat(),
                    end.isoformat(),
                    new_rate,
                    confidence,
                    f"由 {old['stage']} 产能 {offer_id} 转正：{basis.strip()}",
                    "promotion",
                    offer_id,
                    actor["id"],
                    now_iso(),
                ),
            )
            self.conn.execute(
                "update capacity_offers set status='superseded', superseded_by=? "
                "where id=?",
                (nid, offer_id),
            )
            self.conn.execute(
                "update wells set status=? where id=?",
                (well_status, old["well_id"]),
            )
        self.supply_changed(actor, f"产能 {offer_id} 转正为 {next_stage}（{nid}）")
        return self.get_offer(nid)

    # -- 撤销产能（长期封井/报废等）-------------------------------------------
    def withdraw(self, actor: dict, offer_id: str, reason: str) -> dict:
        offer = self._get_offer_row(offer_id)
        if offer["status"] != "active":
            raise ConflictError("只有生效中的产能才能撤销")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("撤销产能必须说明原因")
        with self.conn:
            self.conn.execute(
                "update capacity_offers set status='withdrawn', withdrawn_at=?, "
                "withdraw_reason=? where id=?",
                (now_iso(), reason.strip(), offer_id),
            )
            self.conn.execute(
                "update wells set status='shut_in' where id=?", (offer["well_id"],)
            )
        self.supply_changed(actor, f"产能 {offer_id} 撤销：{reason.strip()}")
        return self.get_offer(offer_id)

    # -- 产能例外 -------------------------------------------------------------
    def submit_exception(
        self,
        actor: dict,
        well_id: str,
        rate: float,
        valid_from: str,
        valid_to: str,
        confidence: str,
        reason: str,
    ) -> dict:
        self._assert_well(well_id)
        start, end = self._validate_window(valid_from, valid_to)
        if confidence not in CONFIDENCE_LEVELS:
            raise ValidationError("置信度必须是 high / medium / low")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("产能例外必须说明理由")
        eid = next_id(self.conn, "EXC")
        with self.conn:
            self.conn.execute(
                "insert into capacity_exceptions(id,well_id,rate,valid_from,valid_to,"
                "confidence,reason,submitted_by,submitted_at,status) "
                "values(?,?,?,?,?,?,?,?,?, 'pending')",
                (
                    eid,
                    well_id,
                    positive_rate(rate, "例外产能"),
                    start.isoformat(),
                    end.isoformat(),
                    confidence,
                    reason.strip(),
                    actor["id"],
                    now_iso(),
                ),
            )
        return self.get_exception(eid)

    def review_exception(
        self, actor: dict, exception_id: str, approve: bool, note: str = ""
    ) -> dict:
        exc = self.conn.execute(
            "select * from capacity_exceptions where id=?", (exception_id,)
        ).fetchone()
        if exc is None:
            raise NotFoundError("产能例外不存在")
        if exc["status"] != "pending":
            raise ConflictError("该产能例外已审批")
        # 职责分离：地质人员不能批准自己提交的产能例外
        if actor["id"] == exc["submitted_by"]:
            raise PermissionError("不能审批自己提交的产能例外")

        with self.conn:
            if not approve:
                self.conn.execute(
                    "update capacity_exceptions set status='rejected', reviewed_by=?, "
                    "reviewed_at=?, review_note=? where id=?",
                    (actor["id"], now_iso(), note or None, exception_id),
                )
                return self.get_exception(exception_id)

            self._assert_source_unused("exception", exception_id)
            oid = next_id(self.conn, "CAP")
            self.conn.execute(
                "insert into capacity_offers(id,well_id,stage,valid_from,valid_to,"
                "rate,confidence,basis,source_type,source_id,created_by,created_at,"
                "status) values(?,?,?,?,?,?,?,?,?,?,?,?, 'active')",
                (
                    oid,
                    exc["well_id"],
                    "exception",
                    exc["valid_from"],
                    exc["valid_to"],
                    exc["rate"],
                    exc["confidence"],
                    f"产能例外 {exception_id}：{exc['reason']}",
                    "exception",
                    exception_id,
                    actor["id"],
                    now_iso(),
                ),
            )
            # 例外产能与原口径不并存：既有 active 产能被更替，杜绝重复计数
            self.conn.execute(
                "update capacity_offers set status='superseded', superseded_by=? "
                "where well_id=? and status='active' and id<>?",
                (oid, exc["well_id"], oid),
            )
            self.conn.execute(
                "update capacity_exceptions set status='approved', reviewed_by=?, "
                "reviewed_at=?, review_note=? where id=?",
                (actor["id"], now_iso(), note or None, exception_id),
            )
        result = self.get_exception(exception_id)
        result["offer_id"] = oid
        self.supply_changed(actor, f"产能例外 {exception_id} 批准生效（{oid}）")
        return result

    # -- 查询 -----------------------------------------------------------------
    def get_offer(self, offer_id: str) -> dict:
        return dict(self._get_offer_row(offer_id))

    def get_exception(self, exception_id: str) -> dict:
        row = self.conn.execute(
            "select * from capacity_exceptions where id=?", (exception_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("产能例外不存在")
        return dict(row)

    def list_offers(self, status: str | None = None) -> list[dict]:
        sql = "select * from capacity_offers"
        params: tuple = ()
        if status:
            sql += " where status=?"
            params = (status,)
        sql += " order by created_at, id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def list_exceptions(self, status: str | None = None) -> list[dict]:
        sql = "select * from capacity_exceptions"
        params: tuple = ()
        if status:
            sql += " where status=?"
            params = (status,)
        sql += " order by submitted_at, id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def active_offer_on(self, well_id: str, day: date) -> sqlite3.Row | None:
        """该井在指定日期适用的唯一产能声明（按时间窗过滤）。"""
        return self.conn.execute(
            "select * from capacity_offers where well_id=? and status='active' "
            "and valid_from<=? and valid_to>=?",
            (well_id, day.isoformat(), day.isoformat()),
        ).fetchone()

    # -- 内部 -----------------------------------------------------------------
    def _validate_window(self, valid_from: str, valid_to: str) -> tuple[date, date]:
        start = parse_date(valid_from, "生效日期")
        end = parse_date(valid_to, "失效日期")
        if start > end:
            raise ValidationError("生效日期不能晚于失效日期")
        return start, end

    def _assert_well(self, well_id: str) -> None:
        if self.conn.execute(
            "select 1 from wells where id=?", (well_id,)
        ).fetchone() is None:
            raise NotFoundError("井不存在")

    def _assert_no_active(self, well_id: str, *, lock: bool = False) -> None:
        row = self.conn.execute(
            "select id from capacity_offers where well_id=? and status='active'",
            (well_id,),
        ).fetchone()
        if row is not None:
            raise ConflictError(
                "该井已有生效中的产能声明；口径变化必须走转正/更替，不得新增重复承诺",
                details={"active_offer": row["id"]},
            )

    def _assert_source_unused(self, source_type: str, source_id: str) -> None:
        row = self.conn.execute(
            "select id from capacity_offers where source_type=? and source_id=?",
            (source_type, source_id),
        ).fetchone()
        if row is not None:
            raise ConflictError("该来源已登记过产能，不能再次累计", details={
                "existing_offer": row["id"]
            })

    def _get_offer_row(self, offer_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from capacity_offers where id=?", (offer_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("产能声明不存在")
        return row
