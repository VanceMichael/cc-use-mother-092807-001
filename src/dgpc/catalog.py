"""主数据：区块、井、储层、外输通道与路由。"""

from __future__ import annotations

import sqlite3

from .errors import ConflictError, NotFoundError, ValidationError
from .util import next_id, positive_rate

WELL_STATUSES = frozenset(
    {"planned", "testing", "trial", "stable", "shut_in"}
)


class CatalogService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # 由 ServiceHub 注入：井接入通道改变可外输能力 -> 重算未来安排
        self.supply_changed = lambda actor, reason: None

    # -- 区块 / 井 -----------------------------------------------------------
    def create_block(self, name: str) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("区块名称不能为空")
        bid = next_id(self.conn, "BLK")
        with self.conn:
            self.conn.execute(
                "insert into blocks(id,name) values(?,?)", (bid, name.strip())
            )
        return self.get_block(bid)

    def get_block(self, block_id: str) -> dict:
        row = self.conn.execute(
            "select * from blocks where id=?", (block_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("区块不存在")
        result = dict(row)
        result["wells"] = [
            dict(r)
            for r in self.conn.execute(
                "select * from wells where block_id=? order by id", (block_id,)
            )
        ]
        return result

    def list_blocks(self) -> list[dict]:
        return [
            self.get_block(r["id"])
            for r in self.conn.execute("select id from blocks order by id")
        ]

    def create_well(self, block_id: str, name: str) -> dict:
        if self.conn.execute(
            "select 1 from blocks where id=?", (block_id,)
        ).fetchone() is None:
            raise NotFoundError("区块不存在")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("井名不能为空")
        if self.conn.execute(
            "select 1 from wells where block_id=? and name=?",
            (block_id, name.strip()),
        ).fetchone():
            raise ConflictError("该区块下已存在同名井")
        wid = next_id(self.conn, "WEL")
        with self.conn:
            self.conn.execute(
                "insert into wells(id,block_id,name,status) values(?,?,?, 'planned')",
                (wid, block_id, name.strip()),
            )
        return self.get_well(wid)

    def get_well(self, well_id: str) -> dict:
        row = self.conn.execute(
            "select * from wells where id=?", (well_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("井不存在")
        return dict(row)

    def list_wells(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute("select * from wells order by id")
        ]

    def set_well_status(self, well_id: str, status: str) -> dict:
        self.get_well(well_id)
        if status not in WELL_STATUSES:
            raise ValidationError(f"井状态非法：{status}")
        with self.conn:
            self.conn.execute(
                "update wells set status=? where id=?", (status, well_id)
            )
        return self.get_well(well_id)

    # -- 外输通道 / 路由 ------------------------------------------------------
    def create_channel(self, name: str, capacity: float) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("通道名称不能为空")
        cid = next_id(self.conn, "CHN")
        with self.conn:
            self.conn.execute(
                "insert into export_channels(id,name,capacity) values(?,?,?)",
                (cid, name.strip(), positive_rate(capacity, "通道能力")),
            )
        return self.get_channel(cid)

    def get_channel(self, channel_id: str) -> dict:
        row = self.conn.execute(
            "select * from export_channels where id=?", (channel_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("外输通道不存在")
        return dict(row)

    def list_channels(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "select * from export_channels order by id"
            )
        ]

    def attach_route(
        self, well_id: str, channel_id: str, share: float = 1.0,
        actor: dict | None = None,
    ) -> None:
        self.get_well(well_id)
        self.get_channel(channel_id)
        if not isinstance(share, (int, float)) or isinstance(share, bool):
            raise ValidationError("路由比例必须是数字")
        if not 0 < share <= 1:
            raise ValidationError("路由比例应在 (0,1] 区间")
        with self.conn:
            self.conn.execute(
                "insert into well_routes(well_id,channel_id,share) values(?,?,?) "
                "on conflict(well_id,channel_id) do update set share=excluded.share",
                (well_id, channel_id, float(share)),
            )
        self.supply_changed(
            actor or {"id": "SYSTEM", "role": "dispatch"},
            f"井 {well_id} 接入通道 {channel_id}")

    def routes_of_well(self, well_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "select r.*, c.name channel_name, c.capacity channel_capacity "
                "from well_routes r join export_channels c on c.id=r.channel_id "
                "where r.well_id=? order by c.id",
                (well_id,),
            )
        ]

    def assert_well_exists(self, well_id: str) -> dict:
        return self.get_well(well_id)

    def assert_channel_exists(self, channel_id: str) -> dict:
        return self.get_channel(channel_id)
