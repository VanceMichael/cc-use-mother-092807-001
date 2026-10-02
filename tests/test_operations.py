"""作业方案、停井/复产与检修锁：只影响尚未履行的安排。"""

from src.deep_gas import operations, supply, testing
from src.deep_gas.errors import AuthorizationError, Conflict

from domain_fixture import DomainFixture


class OperationsTest(DomainFixture):
    def setUp(self) -> None:
        super().setUp()
        testing.register_baseline(self.conn, self.actors["disp"], "W1", 50.0, "2026-09-01")

    def test_shutdown_only_affects_future_dates(self) -> None:
        # 停井窗口 10-10..10-12
        operations.create_plan(self.conn, self.actors["maint"], "W1", "acidizing", "P1")
        operations.approve_plan(self.conn, self.actors["disp"], "P1")
        operations.record_shutdown(self.conn, self.actors["maint"], "P1",
                                   "2026-10-10", "2026-10-12")
        before = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-09")
        during = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-11")
        after = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-13")
        self.assertEqual(before["total_available"], 50.0)
        self.assertEqual(during["total_available"], 0.0)
        self.assertEqual(after["total_available"], 50.0)
        self.assertEqual(during["wells"][0]["block_reason"]["kind"], "work_window")

    def test_revival_adds_incremental_gain_and_closes_window(self) -> None:
        operations.create_plan(self.conn, self.actors["maint"], "W1", "workover", "P2")
        operations.approve_plan(self.conn, self.actors["disp"], "P2")
        operations.record_shutdown(self.conn, self.actors["maint"], "P2",
                                   "2026-10-10", None)
        operations.record_revival(self.conn, self.actors["maint"], "P2",
                                  "2026-10-13", expected_gain=12.0, confidence="high")
        result = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-13")
        # 基线 50 + 复产增量 12，且高置信计入 firm
        self.assertEqual(result["firm_available"], 62.0)

    def test_plan_proposer_cannot_approve_own_plan(self) -> None:
        operations.create_plan(self.conn, self.actors["maint"], "W1", "other", "P3")
        with self.assertRaises(Conflict):
            operations.approve_plan(self.conn, self.actors["maint"], "P3")

    def test_lock_requires_maintenance_to_unlock(self) -> None:
        lock = operations.create_lock(
            self.conn, self.actors["maint"], "2026-10-20", "2026-10-21", "阀检",
            well_code="W1")
        with self.assertRaises(AuthorizationError):
            operations.unlock(self.conn, self.actors["disp"], lock["id"])
        blocked = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-20")
        self.assertEqual(blocked["total_available"], 0.0)
        operations.unlock(self.conn, self.actors["maint"], lock["id"])
        freed = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-20")
        self.assertEqual(freed["total_available"], 50.0)

    def test_channel_restriction_curtails_within_window_only(self) -> None:
        # W1 产能 50，通道基础能力 200；限输到 30 只在 10-20 当天
        supply.add_restriction(self.conn, self.actors["pipe"], "C1", 30.0,
                               "2026-10-20", "2026-10-20", "下游检修")
        inside = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-20")
        outside = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-21")
        self.assertEqual(inside["total_available"], 30.0)
        self.assertEqual(outside["total_available"], 50.0)
        self.assertTrue(inside["channels"][0]["curtailed"])
