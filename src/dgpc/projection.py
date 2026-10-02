"""可靠供应测算。

给定日期，逐井给出可承诺产量：
1. 产能必须 active 且时间窗覆盖当日；
2. 当日处于作业/检修阻断的井产量为 0；
3. 逐通道汇总外输需求，遇到生效限输时按比例削减通道内各井；
4. 低置信（low）产能只作参考量，不计入可对承诺担保的可靠供应。

测算结果同时返回每条结论的依据，供供气决定快照与追溯使用。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from .operations import OperationsService

# 可对供气承诺担保的置信等级；low 仅作参考
FIRM_CONFIDENCE = frozenset({"high", "medium"})


class ProjectionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        operations: OperationsService | None = None,
    ) -> None:
        self.conn = conn
        self.operations = operations or OperationsService(conn)

    def reliable_supply(self, day: date) -> dict[str, Any]:
        iso = day.isoformat()
        self.operations.refresh_curtailment_states(day)

        wells = [dict(r) for r in self.conn.execute(
            "select * from wells order by id"
        )]
        well_rows: dict[str, dict] = {}
        for well in wells:
            offer = self.conn.execute(
                "select * from capacity_offers where well_id=? and status='active' "
                "and valid_from<=? and valid_to>=?",
                (well["id"], iso, iso),
            ).fetchone()
            blockers = self.operations.well_blockers(well["id"], day)
            routes = [dict(r) for r in self.conn.execute(
                "select r.*, c.name channel_name, c.capacity channel_capacity "
                "from well_routes r join export_channels c on c.id=r.channel_id "
                "where r.well_id=? order by c.id",
                (well["id"],),
            )]
            no_offer_reasons = ["当日无适用的生效产能声明"]
            if blockers:
                no_offer_reasons.append(
                    "作业/检修停井：" + "、".join(
                        f"{b['type']}:{b['id']}" for b in blockers
                    )
                )
            if offer is None:
                well_rows[well["id"]] = {
                    "well_id": well["id"],
                    "well_name": well["name"],
                    "gross_rate": 0.0,
                    "firm_rate": 0.0,
                    "base_firm_rate": 0.0,
                    "exportable_rate": 0.0,
                    "status": well["status"],
                    "reasons": no_offer_reasons,
                    "offer": None,
                    "blockers": blockers,
                    "routes": routes,
                }
                continue
            rate = offer["rate"]
            reasons = [
                f"产能 {offer['id']}（{offer['stage']}，{offer['confidence']}）"
                f"有效期 {offer['valid_from']}~{offer['valid_to']}"
            ]
            firm = rate if offer["confidence"] in FIRM_CONFIDENCE else 0.0
            if offer["confidence"] not in FIRM_CONFIDENCE:
                reasons.append(f"置信度 {offer['confidence']} 不计入可靠供应")
            if blockers:
                rate = 0.0
                firm = 0.0
                reasons.append(
                    "作业/检修停井：" + "、".join(
                        f"{b['type']}:{b['id']}" for b in blockers
                    )
                )
            if rate > 0 and not routes:
                reasons.append("未接入外输通道，产能不能外输")
            well_rows[well["id"]] = {
                "well_id": well["id"],
                "well_name": well["name"],
                "gross_rate": round(rate, 4),
                "firm_rate": round(firm, 4),  # 通道平衡后覆写为可外输量
                "base_firm_rate": round(firm, 4),
                "exportable_rate": 0.0,  # 通道平衡后回填
                "status": well["status"],
                "reasons": reasons,
                "offer": dict(offer),
                "blockers": blockers,
                "routes": routes,
            }

        # 通道平衡：需求 vs 通道能力（含当日生效限输）
        channel_rows = [dict(r) for r in self.conn.execute(
            "select * from export_channels order by id"
        )]
        channel_view: dict[str, dict] = {}
        for ch in channel_rows:
            cur = self.conn.execute(
                "select * from curtailments where channel_id=? and status='active' "
                "and valid_from<=? and valid_to>=? order by id limit 1",
                (ch["id"], iso, iso),
            ).fetchone()
            effective_cap = cur["limit_rate"] if cur else ch["capacity"]
            view = {
                "channel_id": ch["id"],
                "channel_name": ch["name"],
                "nominal_capacity": ch["capacity"],
                "effective_capacity": round(effective_cap, 4),
                "curtailment": dict(cur) if cur else None,
                "firm_demand": 0.0,
                "gross_demand": 0.0,
                "factor": 1.0,
            }
            channel_view[ch["id"]] = view

        # 汇总需求（按路由份额）
        for wid, row in well_rows.items():
            for rt in row["routes"]:
                view = channel_view[rt["channel_id"]]
                view["firm_demand"] += row["firm_rate"] * rt["share"]
                view["gross_demand"] += row["gross_rate"] * rt["share"]

        # 通道削减系数（firm/gross 共用同一物理系数，按总需求计算）
        for view in channel_view.values():
            demand = max(view["firm_demand"], view["gross_demand"])
            if demand > view["effective_capacity"] and demand > 0:
                view["factor"] = round(view["effective_capacity"] / demand, 6)

        # 回填每井可外输量（firm 与 gross 分别按份额×系数汇总）
        totals = {"gross": 0.0, "firm": 0.0}
        for wid, row in well_rows.items():
            firm_export = 0.0
            gross_export = 0.0
            cut_channels: list[str] = []
            for rt in row["routes"]:
                view = channel_view[rt["channel_id"]]
                factor = view["factor"]
                firm_export += row["firm_rate"] * rt["share"] * factor
                gross_export += row["gross_rate"] * rt["share"] * factor
                if factor < 1:
                    cut_channels.append(
                        f"{view['channel_name']}限输系数{factor:g}"
                    )
            if cut_channels:
                row["reasons"].append("通道限输削减：" + "、".join(cut_channels))
            row["exportable_rate"] = round(firm_export, 4)
            row["firm_rate"] = round(firm_export, 4)
            row["gross_rate"] = round(gross_export, 4)
            totals["gross"] += gross_export
            totals["firm"] += firm_export

        return {
            "gas_date": iso,
            "firm_total": round(totals["firm"], 4),
            "gross_total": round(totals["gross"], 4),
            "wells": [well_rows[k] for k in sorted(well_rows)],
            "channels": [channel_view[k] for k in sorted(channel_view)],
        }
