"""从某日供气数字追溯到井况、作业决定与责任人。

trace_gas_day 返回：
- 当日全部（或合同可见的）承诺与逐版供气决定；
- 每条安排对应的井、适用产能、储层解释版本、测试批次/材料、
  当日作业/检修、通道与限输；
- 各环节责任人（提交人、复核人、决定人、解锁人等）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .util import json_loads, parse_date


class TraceService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def trace_gas_day(self, actor: dict, gas_date: str) -> dict[str, Any]:
        day = parse_date(gas_date, "供气日期")
        iso = day.isoformat()
        sql = (
            "select c.* from supply_commitments c join contracts t "
            "on c.contract_id=t.id where c.gas_date=?"
        )
        params: list[Any] = [iso]
        if actor["role"] == "commercial":
            sql += " and t.user_id=?"
            params.append(actor["id"])
        sql += " order by c.id"
        commitments = [
            self.trace_commitment(actor, row["id"])
            for row in self.conn.execute(sql, params)
        ]
        return {
            "gas_date": iso,
            "viewer": actor["id"],
            "viewer_role": actor["role"],
            "commitments": commitments,
        }

    def trace_commitment(self, actor: dict, commitment_id: str) -> dict[str, Any]:
        com = self.conn.execute(
            "select * from supply_commitments where id=?", (commitment_id,)
        ).fetchone()
        if com is None:
            from .errors import NotFoundError

            raise NotFoundError("供气承诺不存在")
        contract = self.conn.execute(
            "select * from contracts where id=?", (com["contract_id"],)
        ).fetchone()
        if actor["role"] == "commercial" and contract["user_id"] != actor["id"]:
            from .errors import PermissionError

            raise PermissionError("只能查看与本合同有关的供气信息")

        decisions = [
            self._decision_detail(r["id"])
            for r in self.conn.execute(
                "select id from supply_decisions where commitment_id=? "
                "order by version",
                (commitment_id,),
            )
        ]
        arrangements = [
            self._arrangement_detail(actor, r, com["gas_date"])
            for r in self.conn.execute(
                "select * from supply_arrangements where commitment_id=? "
                "order by well_id",
                (commitment_id,),
            )
        ]
        gaps = [
            self._redact_gap(dict(r))
            for r in self.conn.execute(
                "select * from commitment_gaps where commitment_id=? order by id",
                (commitment_id,),
            )
        ]
        result = {
            "commitment": dict(com),
            "contract": dict(contract),
            "creator": self._person(com["created_by"]),
            "decisions": decisions,
            "arrangements": arrangements,
            "gaps": gaps,
        }
        if actor["role"] == "commercial":
            # 商业用户只保留与本合同交付通道有关的测算内容
            channel_id = contract["channel_id"]
            for decision in decisions:
                decision["snapshot"] = self._sanitize_snapshot(
                    decision["snapshot"], channel_id)
        return result

    @staticmethod
    def _redact_gap(gap: dict) -> dict:
        return gap

    @staticmethod
    def _sanitize_snapshot(snapshot: dict, channel_id: str) -> dict:
        """删除与该合同交付通道无关的井/通道信息。"""
        projection = snapshot.get("projection") or {}
        wells = [
            w for w in projection.get("wells", [])
            if any(rt["channel_id"] == channel_id for rt in w.get("routes", []))
        ]
        channels = [
            c for c in projection.get("channels", [])
            if c.get("channel_id") == channel_id
        ]
        sanitized_projection = {
            **projection,
            "wells": wells,
            "channels": channels,
        }
        return {
            "gas_date": snapshot.get("gas_date"),
            "volume": snapshot.get("volume"),
            "projection": sanitized_projection,
            "arrangements": snapshot.get("arrangements", []),
            "well_notes": {
                wid: notes
                for wid, notes in (snapshot.get("well_notes") or {}).items()
                if wid in {w["well_id"] for w in wells}
            },
        }

    def trace_well_day(self, well_id: str, gas_date: str) -> dict[str, Any]:
        """单井在某日的完整画像（内部使用）。"""
        iso = parse_date(gas_date, "供气日期").isoformat()
        well = self.conn.execute(
            "select * from wells where id=?", (well_id,)
        ).fetchone()
        if well is None:
            from .errors import NotFoundError

            raise NotFoundError("井不存在")
        offer = self.conn.execute(
            "select * from capacity_offers where well_id=? and status='active' "
            "and valid_from<=? and valid_to>=?",
            (well_id, iso, iso),
        ).fetchone()
        result: dict[str, Any] = {"well": dict(well)}
        result["offer"] = self._offer_chain(offer["id"]) if offer else None
        result["interpretations"] = [
            dict(r)
            for r in self.conn.execute(
                "select * from reservoir_interpretations where well_id=? "
                "order by version desc",
                (well_id,),
            )
        ]
        result["test_batches"] = [
            self._test_detail(r["id"])
            for r in self.conn.execute(
                "select id from test_batches where well_id=? order by submitted_at",
                (well_id,),
            )
        ]
        result["programs"] = [
            self._program_detail(dict(r))
            for r in self.conn.execute(
                "select * from work_programs where well_id=? and "
                "planned_start<=? and (planned_end>=? or status='in_progress') "
                "order by id",
                (well_id, iso, iso),
            )
        ]
        result["maintenance"] = [
            self._window_detail(dict(r))
            for r in self.conn.execute(
                "select * from maintenance_windows where well_id=? and "
                "status in ('locked','await_unlock') and lock_from<=? order by id",
                (well_id, iso),
            )
        ]
        result["routes"] = [
            dict(r)
            for r in self.conn.execute(
                "select r.*, c.name channel_name, c.capacity channel_capacity "
                "from well_routes r join export_channels c on c.id=r.channel_id "
                "where r.well_id=? order by c.id",
                (well_id,),
            )
        ]
        result["reports"] = [
            dict(r)
            for r in self.conn.execute(
                "select * from daily_reports where well_id=? and gas_date=? "
                "order by version",
                (well_id, iso),
            )
        ]
        return result

    # == 内部明细组装 =========================================================
    def _arrangement_detail(
        self, actor: dict, row: sqlite3.Row, gas_date: str
    ) -> dict[str, Any]:
        detail = dict(row)
        well = self.conn.execute(
            "select * from wells where id=?", (row["well_id"],)
        ).fetchone()
        detail["well"] = dict(well)
        offer = self.conn.execute(
            "select * from capacity_offers where well_id=? and status='active' "
            "and valid_from<=? and valid_to>=?",
            (row["well_id"], gas_date, gas_date),
        ).fetchone()
        detail["effective_offer"] = self._offer_chain(offer["id"]) if offer else None
        blockers = []
        for p in self.conn.execute(
            "select * from work_programs where well_id=? and status in "
            "('scheduled','in_progress') and planned_start<=? "
            "and (planned_end>=? or status='in_progress')",
            (row["well_id"], gas_date, gas_date),
        ):
            blockers.append(self._program_detail(dict(p)))
        for w in self.conn.execute(
            "select * from maintenance_windows where well_id=? and status in "
            "('locked','await_unlock') and lock_from<=?",
            (row["well_id"], gas_date),
        ):
            blockers.append(self._window_detail(dict(w)))
        detail["active_blockers"] = blockers
        return detail

    def _offer_chain(self, offer_id: str) -> dict[str, Any]:
        offer = self.conn.execute(
            "select * from capacity_offers where id=?", (offer_id,)
        ).fetchone()
        result = dict(offer)
        result["created_by_person"] = self._person(offer["created_by"])
        result["lineage"] = self._lineage(offer)
        result["exception"] = None
        if offer["source_type"] == "exception":
            exc = self.conn.execute(
                "select * from capacity_exceptions where id=?",
                (offer["source_id"],),
            ).fetchone()
            if exc:
                ed = dict(exc)
                ed["submitted_by_person"] = self._person(exc["submitted_by"])
                ed["reviewed_by_person"] = self._person(exc["reviewed_by"])
                result["exception"] = ed
        return result

    def _lineage(self, offer: sqlite3.Row) -> list[dict[str, Any]]:
        """沿 promotion 来源向上回溯测试批次/解释等最初依据。"""
        chain: list[dict[str, Any]] = []
        current = offer
        seen: set[str] = set()
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            node = {
                "offer_id": current["id"],
                "stage": current["stage"],
                "rate": current["rate"],
                "valid_from": current["valid_from"],
                "valid_to": current["valid_to"],
                "status": current["status"],
                "basis": current["basis"],
                "confidence": current["confidence"],
                "created_by": self._person(current["created_by"]),
            }
            if current["source_type"] in ("test_batch", "reopen_test"):
                node["test_batch"] = self._test_detail(current["source_id"])
            chain.append(node)
            if current["source_type"] == "promotion":
                current = self.conn.execute(
                    "select * from capacity_offers where id=?",
                    (current["source_id"],),
                ).fetchone()
            else:
                break
        chain.reverse()
        return chain

    def _test_detail(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.conn.execute(
            "select * from test_batches where id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            return None
        detail = dict(batch)
        detail["submitted_by_person"] = self._person(batch["submitted_by"])
        detail["reviewed_by_person"] = self._person(batch["reviewed_by"])
        interp = self.conn.execute(
            "select * from reservoir_interpretations where id=?",
            (batch["interpretation_id"],),
        ).fetchone()
        if interp:
            idict = dict(interp)
            idict["submitted_by_person"] = self._person(interp["submitted_by"])
            idict["payload"] = json_loads(interp["payload"])
            detail["interpretation"] = idict
        if batch["material_id"]:
            m = self.conn.execute(
                "select id,material_no,kind,status,payload_hash,"
                "chosen_variant_id from inbound_materials where id=?",
                (batch["material_id"],),
            ).fetchone()
            detail["material"] = dict(m) if m else None
        return detail

    def _program_detail(self, prog: dict) -> dict[str, Any]:
        prog["created_by_person"] = self._person(prog.get("created_by"))
        prog["revisions"] = [
            dict(r)
            for r in self.conn.execute(
                "select * from work_program_revisions where program_id=? "
                "order by changed_at",
                (prog["id"],),
            )
        ]
        return prog

    def _window_detail(self, win: dict) -> dict[str, Any]:
        win["created_by_person"] = self._person(win.get("created_by"))
        win["unlocked_by_person"] = self._person(win.get("unlocked_by"))
        win["revisions"] = [
            dict(r)
            for r in self.conn.execute(
                "select * from maintenance_revisions where window_id=? "
                "order by changed_at",
                (win["id"],),
            )
        ]
        return win

    def _decision_detail(self, decision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "select * from supply_decisions where id=?", (decision_id,)
        ).fetchone()
        result = dict(row)
        result["snapshot"] = json_loads(row["snapshot"])
        result["decided_by_person"] = self._person(row["decided_by"])
        return result

    def _person(self, user_id: str | None) -> dict[str, Any] | None:
        if not user_id:
            return None
        row = self.conn.execute(
            "select id,name,role from users where id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else {"id": user_id, "name": "未知", "role": "unknown"}
