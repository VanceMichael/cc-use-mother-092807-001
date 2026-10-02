"""供气承诺、供气决定、安排重规划与承诺缺口提醒。

业务链：
- 供气承诺按合同+供气日登记，合同绑定交付外输通道；登记瞬间基于可靠
  供应测算落 v1 供气决定（快照井况、作业、检修、通道与逐井安排）；
- 同一通道、同一供气日的多个承诺按登记顺序从同一能力池分配，已被其他
  承诺占用的能力不能重复承诺；能力不足立即生成 open 缺口提醒；
- 维修停井、酸化/压裂、管网限输等事件只重算**尚未履行**（供气日未到，
  或当日尚未形成日报、状态 planned/revised）的安排；已经形成的日报与
  供气决定不改动，只通过决定更正版本衔接；
- 供应恢复后开放缺口自动消除；调度也可手工关闭缺口；
- 调度可发布供气决定更正版（旧版 superseded，快照永久保留）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .errors import ConflictError, NotFoundError, PermissionError, ValidationError
from .projection import ProjectionService
from .util import json_dumps, json_loads, next_id, now_iso, parse_date, positive_rate

# 重规划允许触及的安排状态；fulfilled 已随日报定稿，永久冻结
OPEN_ARRANGEMENT = ("planned", "revised")


class CommitmentService:
    def __init__(self, conn: sqlite3.Connection, projection: ProjectionService) -> None:
        self.conn = conn
        self.projection = projection

    # == 承诺登记 =============================================================
    def create_commitment(
        self,
        actor: dict,
        contract_id: str,
        gas_date: str,
        volume: float,
        note: str = "",
    ) -> dict:
        contract = self._contract_row(contract_id)
        day = parse_date(gas_date, "供气日期")
        if day < date.today():
            raise ValidationError("不能对过去的日期登记供气承诺")
        amount = positive_rate(volume, "承诺量")
        dup = self.conn.execute(
            "select id from supply_commitments where contract_id=? and gas_date=? "
            "and status!='cancelled'",
            (contract_id, day.isoformat()),
        ).fetchone()
        if dup is not None:
            raise ConflictError("该合同当日已有有效供气承诺，请对既有承诺做更正")

        cid = next_id(self.conn, "COM")
        with self.conn:
            self.conn.execute(
                "insert into supply_commitments(id,contract_id,gas_date,volume,status,"
                "created_by,created_at,note) values(?,?,?,?, 'committed',?,?,?)",
                (cid, contract_id, day.isoformat(), amount,
                 actor["id"], now_iso(), note or None),
            )
        self._rebuild_day(actor, day)
        return self.get_commitment(actor, cid)

    def cancel_commitment(self, actor: dict, commitment_id: str, note: str) -> dict:
        com = self._commitment_row(commitment_id)
        if com["status"] == "cancelled":
            raise ConflictError("承诺已取消")
        if parse_date(com["gas_date"], "供气日期") <= date.today():
            raise ConflictError("供气日已到或已过，已形成的安排不能取消，只能更正")
        if not isinstance(note, str) or not note.strip():
            raise ValidationError("取消承诺必须说明原因")
        with self.conn:
            self.conn.execute(
                "update supply_commitments set status='cancelled' where id=?",
                (commitment_id,),
            )
            self.conn.execute(
                "update supply_arrangements set status='cancelled', revised_at=?, "
                "revise_reason=? where commitment_id=? and status in (?,?)",
                (now_iso(), f"承诺取消：{note.strip()}", commitment_id,
                 "planned", "revised"),
            )
            self._close_gaps_for(
                commitment_id, f"承诺取消：{note.strip()}")
        self._rebuild_day(actor, parse_date(com["gas_date"], "供气日期"),
                          reason=f"承诺 {commitment_id} 取消")
        return self.get_commitment(actor, commitment_id)

    # == 事件驱动的重规划（只改尚未履行的安排）================================
    def replan_future(self, actor: dict, reason: str, today: date | None = None) -> int:
        """供应类事件后调用。重排所有未到/当日承诺，返回受影响承诺数。"""
        today = today or date.today()
        rows = self.conn.execute(
            "select distinct gas_date from supply_commitments "
            "where status='committed' and gas_date>=? order by gas_date",
            (today.isoformat(),),
        ).fetchall()
        affected = 0
        for r in rows:
            day = parse_date(r["gas_date"], "供气日期")
            before = self._allocation_signatures(day)
            self._rebuild_day(actor, day, reason=reason)
            after = self._allocation_signatures(day)
            affected += len(after - before)
        return affected

    def correct_decision(
        self, actor: dict, commitment_id: str, note: str
    ) -> dict:
        """调度手工发布供气决定更正版（旧版保留可追溯）。"""
        com = self._commitment_row(commitment_id)
        if com["status"] == "cancelled":
            raise ConflictError("承诺已取消，无可更正的决定")
        if not isinstance(note, str) or not note.strip():
            raise ValidationError("决定更正必须说明原因")
        day = parse_date(com["gas_date"], "供气日期")
        self._rebuild_day(actor, day, reason=note.strip(),
                          force_decision={commitment_id})
        return self.get_commitment(actor, commitment_id)

    # == 日报兑现联动 =========================================================
    def mark_arrangement_actuals(
        self, actor: dict, well_id: str, gas_date_iso: str
    ) -> None:
        """某井某日日报定稿后，对应安排标记为已履行（历史不再变动）。"""
        arrangements = self.conn.execute(
            "select * from supply_arrangements where well_id=? and gas_date=? "
            "and status in (?,?)",
            (well_id, gas_date_iso, "planned", "revised"),
        ).fetchall()
        report = self.conn.execute(
            "select rate from daily_reports where well_id=? and gas_date=? "
            "order by version desc limit 1",
            (well_id, gas_date_iso),
        ).fetchone()
        with self.conn:
            for a in arrangements:
                self.conn.execute(
                    "update supply_arrangements set status='fulfilled', "
                    "actual_rate=? where id=?",
                    (report["rate"] if report else None, a["id"]),
                )
            for cid in {a["commitment_id"] for a in arrangements}:
                pending = self.conn.execute(
                    "select 1 from supply_arrangements where commitment_id=? "
                    "and status in (?,?) limit 1",
                    (cid, "planned", "revised"),
                ).fetchone()
                if pending is None:
                    self.conn.execute(
                        "update supply_commitments set status='fulfilled' "
                        "where id=? and status='committed'",
                        (cid,),
                    )

    # == 查询 =================================================================
    def get_commitment(self, actor: dict, commitment_id: str) -> dict:
        row = self._commitment_row(commitment_id)
        self._assert_visible(actor, row["contract_id"])
        result = dict(row)
        result["contract"] = dict(self._contract_row(row["contract_id"]))
        result["arrangements"] = [
            dict(r) for r in self.conn.execute(
                "select * from supply_arrangements where commitment_id=? "
                "order by well_id",
                (commitment_id,),
            )
        ]
        result["decisions"] = [
            dict(r) for r in self.conn.execute(
                "select id,commitment_id,version,decided_by,decided_at,"
                "reliable_total,allocated_total,status,superseded_by,note "
                "from supply_decisions where commitment_id=? order by version",
                (commitment_id,),
            )
        ]
        result["open_gap"] = self.open_gap(commitment_id)
        return result

    def list_commitments(
        self, actor: dict, *, contract_id: str | None = None
    ) -> list[dict]:
        sql = (
            "select c.id from supply_commitments c join contracts t "
            "on c.contract_id=t.id where 1=1"
        )
        params: list = []
        if actor["role"] == "commercial":
            sql += " and t.user_id=?"
            params.append(actor["id"])
        if contract_id:
            self._assert_visible(actor, contract_id)
            sql += " and c.contract_id=?"
            params.append(contract_id)
        sql += " order by c.gas_date, c.id"
        return [
            self.get_commitment(actor, r["id"])
            for r in self.conn.execute(sql, params)
        ]

    def get_decision(self, value: str | sqlite3.Row) -> dict:
        if isinstance(value, sqlite3.Row):
            row = value
        else:
            row = self.conn.execute(
                "select * from supply_decisions where id=?", (value,)
            ).fetchone()
            if row is None:
                raise NotFoundError("供气决定不存在")
        result = dict(row)
        result["snapshot"] = json_loads(row["snapshot"])
        return result

    def list_open_gaps(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "select * from commitment_gaps where status='open' "
                "order by gas_date, id"
            )
        ]

    def open_gap(self, commitment_id: str) -> dict | None:
        row = self.conn.execute(
            "select * from commitment_gaps where commitment_id=? and status='open'",
            (commitment_id,),
        ).fetchone()
        return dict(row) if row else None

    def close_gap(self, actor: dict, gap_id: str, note: str) -> dict:
        row = self.conn.execute(
            "select * from commitment_gaps where id=?", (gap_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("缺口提醒不存在")
        if row["status"] != "open":
            raise ConflictError("该缺口已关闭/已消除")
        with self.conn:
            self.conn.execute(
                "update commitment_gaps set status='closed', resolved_at=?, note=? "
                "where id=?",
                (now_iso(), note or None, gap_id),
            )
        return dict(self.conn.execute(
            "select * from commitment_gaps where id=?", (gap_id,)
        ).fetchone())

    # == 核心：逐日、逐通道的联合能力分配 ======================================
    def _rebuild_day(
        self,
        actor: dict,
        day: date,
        *,
        reason: str | None = None,
        force_decision: set[str] | None = None,
    ) -> None:
        force_decision = force_decision or set()
        projection = self.projection.reliable_supply(day)
        firm_via = self._firm_by_well_channel(projection)

        com_rows = self.conn.execute(
            "select c.*, t.channel_id from supply_commitments c "
            "join contracts t on t.id=c.contract_id "
            "where c.gas_date=? and c.status='committed' order by c.id",
            (day.isoformat(),),
        ).fetchall()

        # 已被 fulfilled（日报定稿）安排占用的能力视为固定占用
        reserved: dict[str, float] = {}
        frozen = self.conn.execute(
            "select t.channel_id, sum(a.actual_rate) total "
            "from supply_arrangements a join supply_commitments c "
            "on c.id=a.commitment_id join contracts t on t.id=c.contract_id "
            "where a.gas_date=? and a.status='fulfilled' group by t.channel_id",
            (day.isoformat(),),
        ).fetchall()
        for r in frozen:
            reserved[r["channel_id"]] = round(r["total"] or 0.0, 4)

        new_allocations: dict[str, dict[str, float]] = {}
        channel_totals = {
            ch["channel_id"]: round(
                sum(
                    v.get(ch["channel_id"], 0.0)
                    for v in firm_via.values()
                ), 4)
            for ch in projection["channels"]
        }
        for com in com_rows:
            channel_id = com["channel_id"]
            pool = firm_via  # well -> {channel: firm}
            firms = {
                well_id: via[channel_id]
                for well_id, via in pool.items()
                if via.get(channel_id, 0) > 0
            }
            capacity = channel_totals.get(channel_id, 0.0)
            already = reserved.get(channel_id, 0.0)
            available = round(max(0.0, capacity - already), 4)
            allocations = self._allocate(firms, min(com["volume"], available))
            new_allocations[com["id"]] = allocations
            reserved[channel_id] = round(
                already + sum(allocations.values()), 4)

        # 落库（仅触及未履行安排）并决定是否需要新版决定
        can_edit = day >= date.today()
        changed: set[str] = set()
        stamp = now_iso()
        with self.conn:
            for com in com_rows:
                cid = com["id"]
                wanted = new_allocations[cid]
                existing = {
                    r["well_id"]: r
                    for r in self.conn.execute(
                        "select * from supply_arrangements where commitment_id=?",
                        (cid,),
                    )
                }
                if can_edit:
                    for well_id, rate in wanted.items():
                        old = existing.pop(well_id, None)
                        if old is None:
                            self.conn.execute(
                                "insert into supply_arrangements(id,commitment_id,"
                                "gas_date,well_id,planned_rate,status,created_at) "
                                "values(?,?,?,?,?, 'planned',?)",
                                (next_id(self.conn, "ARR"), cid,
                                 day.isoformat(), well_id, rate, stamp),
                            )
                            changed.add(cid)
                        elif old["status"] in OPEN_ARRANGEMENT:
                            if abs(old["planned_rate"] - rate) > 1e-6:
                                self._write_arrangement_revision(
                                    old, rate, reason or "供应条件变化重算",
                                    actor, stamp)
                                changed.add(cid)
                        elif old["status"] == "cancelled":
                            # 供应恢复：曾经因停井/限输取消的安排可以复活
                            self._write_arrangement_revision(
                                old, rate,
                                reason or "供应恢复，安排重新生效",
                                actor, stamp)
                            self.conn.execute(
                                "update supply_arrangements set status='revised' "
                                "where id=?", (old["id"],))
                            changed.add(cid)
                        # fulfilled：随日报定稿，永久冻结
                    for old in existing.values():
                        if old["status"] in OPEN_ARRANGEMENT:
                            self.conn.execute(
                                "update supply_arrangements set status='cancelled', "
                                "revised_at=?, revise_reason=? where id=?",
                                (stamp, reason or "供应条件变化重算", old["id"]),
                            )
                            changed.add(cid)

            for com in com_rows:
                cid = com["id"]
                if cid in changed or not self._has_decision(cid):
                    self._snapshot_decision(
                        actor, cid, projection=projection,
                        version=self._next_decision_version(cid),
                        note=(f"供应条件变化：{reason}" if reason and cid in changed
                              and self._has_decision(cid)
                              else ("供气决定初版" if not self._has_decision(cid)
                                    else note_or(reason, "供应条件变化重算"))),
                    )
                elif cid in force_decision:
                    self._snapshot_decision(
                        actor, cid, projection=projection,
                        version=self._next_decision_version(cid),
                        note=reason or "调度手工更正",
                    )
                self._evaluate_gap(cid, com, projection)

    @staticmethod
    def _firm_by_well_channel(projection: dict) -> dict[str, dict[str, float]]:
        """从测算结果取每井经各通道可交付的 firm 产量（已含限输系数）。"""
        result: dict[str, dict[str, float]] = {}
        for w in projection["wells"]:
            via: dict[str, float] = {}
            for rt in w["routes"]:
                ch = next(
                    c for c in projection["channels"]
                    if c["channel_id"] == rt["channel_id"]
                )
                # 井的 firm 按路由份额分配，再乘通道限输系数
                base = w.get("base_firm_rate", w["firm_rate"])
                via[rt["channel_id"]] = round(
                    base * rt["share"] * ch["factor"], 4)
            result[w["well_id"]] = via
        return result

    @staticmethod
    def _allocate(firms: dict[str, float], target: float) -> dict[str, float]:
        """在指定通道能力额度内按各井 firm 比例分摊；超出部分不硬凑。"""
        total = sum(firms.values())
        if total <= 0 or target <= 0:
            return {}
        goal = round(min(target, total), 4)
        allocations: dict[str, float] = {}
        remaining = goal
        items = sorted(firms.items())
        for idx, (well_id, firm) in enumerate(items):
            if idx == len(items) - 1:
                share = round(min(remaining, firm), 4)  # 末井吸收舍入差且不超井能力
            else:
                share = round(min(goal * firm / total, firm), 4)
                remaining = round(remaining - share, 4)
            if share > 0:
                allocations[well_id] = share
        return allocations

    def _snapshot_decision(
        self,
        actor: dict,
        commitment_id: str,
        *,
        version: int,
        note: str,
        projection: dict | None = None,
    ) -> str:
        com = self._commitment_row(commitment_id)
        if projection is None:
            projection = self.projection.reliable_supply(
                parse_date(com["gas_date"], "供气日期"))
        arrangements = [
            dict(r) for r in self.conn.execute(
                "select * from supply_arrangements where commitment_id=? "
                "and status in ('planned','revised','fulfilled') order by well_id",
                (commitment_id,),
            )
        ]
        allocated = round(sum(a["planned_rate"] for a in arrangements), 4)
        snapshot = {
            "gas_date": com["gas_date"],
            "contract_id": com["contract_id"],
            "volume": com["volume"],
            "projection": projection,
            "arrangements": arrangements,
            "well_notes": {
                w["well_id"]: w["reasons"]
                for w in projection["wells"]
            },
        }
        did = next_id(self.conn, "DEC")
        actor_id = actor.get("id")
        if actor_id in (None, "SYSTEM", "CATALOG", "BOOTSTRAP"):
            actor_id = None  # 系统自动重算生成的决定版本
        with self.conn:
            if version > 1:
                self.conn.execute(
                    "update supply_decisions set status='superseded', superseded_by=? "
                    "where commitment_id=? and status='effective'",
                    (did, commitment_id),
                )
            self.conn.execute(
                "insert into supply_decisions(id,commitment_id,version,decided_by,"
                "decided_at,reliable_total,allocated_total,snapshot,status,note) "
                "values(?,?,?,?,?,?,?,?,'effective',?)",
                (did, commitment_id, version, actor_id, now_iso(),
                 projection["firm_total"], allocated,
                 json_dumps(snapshot), note),
            )
        return did

    def _evaluate_gap(
        self, commitment_id: str, com: sqlite3.Row, projection: dict
    ) -> None:
        decision_row = self.conn.execute(
            "select * from supply_decisions where commitment_id=? "
            "and status='effective'",
            (commitment_id,),
        ).fetchone()
        allocated = decision_row["allocated_total"] if decision_row else 0.0
        shortage = round(com["volume"] - allocated, 4)
        with self.conn:
            gap = self.conn.execute(
                "select * from commitment_gaps where commitment_id=? and status='open'",
                (commitment_id,),
            ).fetchone()
            if shortage > 1e-6:
                reasons = self._gap_reasons(com, projection, allocated)
                text = "；".join(reasons) or "交付通道可靠能力低于承诺量"
                if gap is None:
                    self.conn.execute(
                        "insert into commitment_gaps(id,commitment_id,gas_date,"
                        "shortage,reason,status,detected_at) "
                        "values(?,?,?,?,?, 'open',?)",
                        (next_id(self.conn, "GAP"), commitment_id,
                         com["gas_date"], shortage, text, now_iso()),
                    )
                elif abs(gap["shortage"] - shortage) > 1e-6 or gap["reason"] != text:
                    self.conn.execute(
                        "update commitment_gaps set shortage=?, reason=? where id=?",
                        (shortage, text, gap["id"]),
                    )
            elif gap is not None:
                self.conn.execute(
                    "update commitment_gaps set status='resolved', resolved_at=?, "
                    "note=coalesce(note,'')||? where id=?",
                    (now_iso(), "；供应已恢复，缺口自动消除", gap["id"]),
                )

    @staticmethod
    def _gap_reasons(com: sqlite3.Row, projection: dict, allocated: float) -> list[str]:
        channel_id = com["channel_id"] if "channel_id" in com.keys() else None
        reasons: list[str] = []
        for w in projection["wells"]:
            on_channel = any(
                rt["channel_id"] == channel_id for rt in w["routes"]
            ) if channel_id else True
            if not on_channel:
                continue
            for r in w["reasons"]:
                if "置信度" in r:
                    reasons.append(f"{w['well_name']}低置信产能不计入")
                if "停井" in r:
                    reasons.append(f"{w['well_name']}{r.split('：',1)[-1]}")
                if "未接入" in r:
                    reasons.append(f"{w['well_name']}未接入通道")
                if "限输" in r:
                    reasons.append(f"{w['well_name']}{r.split('：',1)[-1]}")
                if "无适用" in r:
                    reasons.append(f"{w['well_name']}无有效产能")
        # 能力被同通道其他承诺占用
        if not reasons and allocated == 0:
            reasons.append("交付通道能力已被其他供气承诺占用")
        return sorted(set(reasons))

    def _allocation_signatures(self, day: date) -> set[tuple]:
        rows = self.conn.execute(
            "select commitment_id, well_id, planned_rate from supply_arrangements "
            "where gas_date=? and status in ('planned','revised','fulfilled')",
            (day.isoformat(),),
        ).fetchall()
        return {(r["commitment_id"], r["well_id"], round(r["planned_rate"], 4))
                for r in rows}

    def _close_gaps_for(self, commitment_id: str, note: str) -> None:
        for r in self.conn.execute(
            "select id from commitment_gaps where commitment_id=? and status='open'",
            (commitment_id,),
        ).fetchall():
            self.conn.execute(
                "update commitment_gaps set status='closed', resolved_at=?, "
                "note=? where id=?",
                (now_iso(), note, r["id"]),
            )

    # == 内部 =================================================================
    def _write_arrangement_revision(
        self, old: sqlite3.Row, new_rate: float, reason: str,
        actor: dict, stamp: str,
    ) -> None:
        actor_id = actor.get("id")
        if actor_id in (None, "SYSTEM", "CATALOG", "BOOTSTRAP"):
            actor_id = None  # 系统自动重算，无具体操作人
        self.conn.execute(
            "insert into arrangement_revisions(id,arrangement_id,old_rate,"
            "new_rate,reason,changed_by,changed_at) values(?,?,?,?,?,?,?)",
            (next_id(self.conn, "ARE"), old["id"],
             old["planned_rate"], new_rate, reason, actor_id, stamp),
        )
        self.conn.execute(
            "update supply_arrangements set planned_rate=?, revised_at=?, "
            "revise_reason=? where id=?",
            (new_rate, stamp, reason, old["id"]),
        )

    def _has_decision(self, commitment_id: str) -> bool:
        return self.conn.execute(
            "select 1 from supply_decisions where commitment_id=? limit 1",
            (commitment_id,),
        ).fetchone() is not None

    def _next_decision_version(self, commitment_id: str) -> int:
        row = self.conn.execute(
            "select coalesce(max(version),0)+1 v from supply_decisions "
            "where commitment_id=?",
            (commitment_id,),
        ).fetchone()
        return row["v"]

    def _assert_visible(self, actor: dict, contract_id: str) -> None:
        if actor["role"] == "commercial" and contract_id not in actor.get(
            "contract_ids", frozenset()
        ):
            raise PermissionError("只能查看与本合同有关的供气信息")

    def _contract_row(self, contract_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from contracts where id=?", (contract_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("供气合同不存在")
        return row

    def _commitment_row(self, commitment_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from supply_commitments where id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("供气承诺不存在")
        return row


def note_or(value: str | None, default: str) -> str:
    return value if value else default
