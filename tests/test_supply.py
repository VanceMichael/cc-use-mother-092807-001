"""供气决定版本化、缺口提醒与端到端追溯。"""

from src.deep_gas import operations, supply, testing
from src.deep_gas.errors import NotFound

from domain_fixture import DomainFixture


class SupplyDecisionTest(DomainFixture):
    def setUp(self) -> None:
        super().setUp()
        testing.register_baseline(self.conn, self.actors["disp"], "W1", 80.0, "2026-09-01")

    def test_re_evaluation_creates_version_and_keeps_history(self) -> None:
        v1 = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05",
                                        reason="初次评估")
        self.assertEqual(v1["version_no"], 1)
        self.assertEqual(v1["total_available"], 80.0)
        # 新增当日限输 60，只影响尚未履行的安排 → 重新评估产生 v2
        supply.add_restriction(self.conn, self.actors["pipe"], "C1", 60.0,
                               "2026-10-05", "2026-10-05", "临时限输")
        v2 = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05",
                                        reason="限输后重评")
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["total_available"], 60.0)
        # v1 证据快照原样保留
        history = supply.get_decision(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        self.assertEqual([h["version_no"] for h in history["history"]], [1, 2])
        v1_read = supply.get_decision(self.conn, self.actors["disp"], "CM1", "2026-10-05", 1)
        self.assertEqual(v1_read["evidence"]["total_available"], 80.0)
        self.assertEqual(v1_read["evidence"]["reason"] if "reason" in v1_read["evidence"] else
                         v1_read["reason"], "初次评估")

    def test_gap_alert_lifecycle(self) -> None:
        # 需求 100，仅可供 80 → 缺口 20
        supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        gaps = supply.open_gap_alerts(self.conn, self.actors["disp"])
        self.assertEqual([(g["gas_date"], g["gap_volume"]) for g in gaps],
                         [("2026-10-05", 20.0)])
        # 老井措施复产 +25（高置信），重评后缺口自动收口
        operations.create_plan(self.conn, self.actors["maint"], "W1", "workover", "PG")
        operations.approve_plan(self.conn, self.actors["disp"], "PG")
        operations.record_revival(self.conn, self.actors["maint"], "PG",
                                  "2026-10-05", expected_gain=25.0, confidence="high")
        supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        self.assertEqual(supply.open_gap_alerts(self.conn, self.actors["disp"]), [])

    def test_trace_links_number_to_wells_and_responsible(self) -> None:
        supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        trace = supply.trace_supply(self.conn, self.actors["disp"], "CM1", "2026-10-05")
        self.assertEqual(trace["evidence"]["total_available"], 80.0)
        well = trace["evidence"]["wells"][0]
        self.assertEqual(well["well_code"], "W1")
        self.assertEqual(well["contributions"][0]["basis"], "baseline")
        self.assertTrue(well["contributions"][0]["confidence_note"])  # 置信依据可回溯
        parties = {p["actor_id"] for p in trace["responsible_parties"]}
        self.assertIn("disp", parties)

    def test_unknown_decision_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            supply.get_decision(self.conn, self.actors["disp"], "CM1", "2026-09-01")
