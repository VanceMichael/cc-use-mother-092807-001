"""商业用户合同级数据隔离。"""

from src.deep_gas import auth, catalog, supply, testing
from src.deep_gas.errors import AuthorizationError

from domain_fixture import DomainFixture


class CommercialIsolationTest(DomainFixture):
    def test_authorized_buyer_reads_only_own_contract_decision(self) -> None:
        # 第二份合同与第二口井，buyer_b 才有权
        catalog.create_well(self.conn, self.actors["disp"], "B1", "W3", "井三")
        catalog.add_route(self.conn, self.actors["pipe"], "W3", "C1")
        catalog.create_contract(self.conn, self.actors["plan"], "K2", "乙方")
        auth.grant_contract_by_code(self.conn, "K2", "buyer_b")
        supply.create_commitment(self.conn, self.actors["plan"], "K2", "CM2",
                                 40.0, "2026-10-01", "2026-10-31")
        supply.add_commitment_source(self.conn, self.actors["plan"], "CM2", "W3")
        testing.register_baseline(self.conn, self.actors["disp"], "W1", 90.0, "2026-09-01")
        testing.register_baseline(self.conn, self.actors["disp"], "W3", 30.0, "2026-09-01")
        supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        supply.evaluate_commitment(self.conn, self.actors["disp"], "CM2", "2026-10-05")

        ok = supply.get_decision(self.conn, self.actors["buyer_a"], "CM1", "2026-10-05")
        self.assertEqual(ok["evidence"]["contract_code"], "K1")
        with self.assertRaises(AuthorizationError):
            supply.get_decision(self.conn, self.actors["buyer_a"], "CM2", "2026-10-05")
        with self.assertRaises(AuthorizationError):
            supply.get_decision(self.conn, self.actors["buyer_b"], "CM1", "2026-10-05")
        supply.get_decision(self.conn, self.actors["buyer_b"], "CM2", "2026-10-05")

    def test_commercial_sees_only_own_gaps_and_no_internal_writes(self) -> None:
        testing.register_baseline(self.conn, self.actors["disp"], "W1", 10.0, "2026-09-01")
        supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        gaps = supply.open_gap_alerts(self.conn, self.actors["buyer_a"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(supply.open_gap_alerts(self.conn, self.actors["buyer_b"]), [])
        with self.assertRaises(AuthorizationError):
            supply.create_commitment(self.conn, self.actors["buyer_a"], "K1", "CMX",
                                     1.0, "2026-10-01", "2026-10-31")
