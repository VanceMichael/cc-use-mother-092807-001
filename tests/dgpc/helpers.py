"""测试夹具：内存库 + 全套角色用户 + 常用建井流程。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from src.dgpc.db import connect, init_db
from src.dgpc.workflow import ServiceHub


def actor_for(conn, user_id: str) -> dict:
    row = conn.execute("select * from users where id=?", (user_id,)).fetchone()
    actor = dict(row)
    actor["contract_ids"] = frozenset(
        r["id"]
        for r in conn.execute(
            "select id from contracts where user_id=?", (user_id,)
        )
    )
    return actor


class HubCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.hub = ServiceHub(self.conn)
        auth = self.hub.auth
        self.tokens: dict[str, str] = {}
        self.u: dict[str, str] = {}
        for key, name, role in (
            ("rig", "井队值班长", "rig"),
            ("geo", "地质责任师", "geology"),
            ("geo2", "地质复核员", "geology"),
            ("maint", "维护班长", "maintenance"),
            ("pipe", "输气值班", "pipeline"),
            ("disp", "调度值班", "dispatch"),
            ("buyer", "商业客户", "commercial"),
        ):
            uid, token = auth.create_user(name, role)
            self.u[key] = uid
            self.tokens[key] = token
        self.today = date.today()
        self.future = lambda days=1: self.today + timedelta(days=days)
        self.past = lambda days=1: self.today - timedelta(days=days)

    def tearDown(self) -> None:
        self.conn.close()

    # -- 常用构造 ---------------------------------------------------------------
    def actor(self, key: str) -> dict:
        return actor_for(self.conn, self.u[key])

    def make_block_well(self, name: str = "测试井", block: str = "测试区块"):
        b = self.hub.catalog.create_block(block)
        w = self.hub.catalog.create_well(b["id"], name)
        return b, w

    def make_channel(self, name: str = "外输通道", capacity: float = 100.0):
        return self.hub.catalog.create_channel(name, capacity)

    def make_approved_offer(
        self,
        well_id: str,
        rate: float = 50.0,
        *,
        confidence: str = "high",
        actor_key: str = "geo",
        channel=None,
        valid_days: int = 60,
    ) -> tuple[dict, dict]:
        """提交解释→测试→调度复核→登记 tested 产能，返回 (测试批次, 产能)。"""
        interp = self.hub.geology.submit_interpretation(
            self.actor(actor_key), well_id, "栖霞组", {"porosity": 0.07})
        batch = self.hub.geology.submit_test_batch(
            self.actor(actor_key), well_id, rate, self.past(1).isoformat(),
            "一点法稳定试井", confidence, interp["id"])
        self.hub.geology.review_test_batch(
            self.actor("disp"), batch["id"], True, "同意")
        offer = self.hub.capacity.publish_from_test(
            self.actor("disp"), batch["id"],
            self.past(1).isoformat(), self.future(valid_days).isoformat())
        if channel is not None:
            self.hub.catalog.attach_route(well_id, channel["id"], 1.0)
        return batch, offer
