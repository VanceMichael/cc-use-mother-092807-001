"""测试公共夹具：在内存库中构建标准组织与一个供气场景。"""

from __future__ import annotations

import unittest

from src.deep_gas import auth, catalog, supply
from src.deep_gas.db import connect


class DomainFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        # 岗位
        auth.register_actor(self.conn, "disp", "调度员", "dispatch")
        auth.register_actor(self.conn, "geo", "地质员", "geology")
        auth.register_actor(self.conn, "drill", "井队员", "drilling")
        auth.register_actor(self.conn, "maint", "维护员", "maintenance")
        auth.register_actor(self.conn, "pipe", "输气员", "pipeline")
        auth.register_actor(self.conn, "plan", "计划员", "planning")
        auth.register_actor(self.conn, "buyer_a", "甲方采购员", "commercial")
        auth.register_actor(self.conn, "buyer_b", "乙方采购员", "commercial")
        self.actors = {aid: auth.get_actor(self.conn, aid)
                       for aid in ("disp", "geo", "drill", "maint", "pipe",
                                   "plan", "buyer_a", "buyer_b")}
        # 区块/井/通道/合同/承诺
        catalog.create_block(self.conn, self.actors["disp"], "B1", "一号区块")
        catalog.create_well(self.conn, self.actors["disp"], "B1", "W1", "井一")
        catalog.create_well(self.conn, self.actors["disp"], "B1", "W2", "井二")
        catalog.create_channel(self.conn, self.actors["pipe"], "C1", "干线", 200.0)
        catalog.add_route(self.conn, self.actors["pipe"], "W1", "C1")
        catalog.add_route(self.conn, self.actors["pipe"], "W2", "C1")
        catalog.create_contract(self.conn, self.actors["plan"], "K1", "甲方")
        auth.grant_contract_by_code(self.conn, "K1", "buyer_a")
        supply.create_commitment(self.conn, self.actors["plan"], "K1", "CM1",
                                 100.0, "2026-10-01", "2026-10-31")
        supply.add_commitment_source(self.conn, self.actors["plan"], "CM1", "W1")

    def tearDown(self) -> None:
        self.conn.close()
