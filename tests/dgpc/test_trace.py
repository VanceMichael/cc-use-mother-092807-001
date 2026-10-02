"""从供气数字追溯到井况、作业决定与责任人。"""

from tests.dgpc.helpers import HubCase


class TraceTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.well = self.make_block_well("西1")
        self.channel = self.make_channel("西干线", 100.0)
        self.batch, self.offer = self.make_approved_offer(
            self.well["id"], 55.0, channel=self.channel)
        self.contract = self.hub.auth.create_contract(
            "甲合同", self.u["buyer"], self.channel["id"])
        self.commitment = self.hub.commitments.create_commitment(
            self.actor("disp"), self.contract,
            self.future(3).isoformat(), 40.0)

    def test_trace_commitment_lineage_and_responsibles(self) -> None:
        t = self.hub.trace.trace_commitment(
            self.actor("disp"), self.commitment["id"])
        self.assertEqual(t["creator"]["role"], "dispatch")
        self.assertEqual(len(t["decisions"]), 1)
        arrangements = t["arrangements"]
        self.assertTrue(arrangements)
        detail = arrangements[0]
        self.assertEqual(detail["well"]["id"], self.well["id"])
        chain = detail["effective_offer"]["lineage"]
        # 产能血缘回溯到测试批次，测试批次再带解释版本与复核责任人
        self.assertTrue(chain)
        test = chain[0]["test_batch"]
        self.assertEqual(test["id"], self.batch["id"])
        self.assertEqual(test["reviewed_by_person"]["role"], "dispatch")
        self.assertEqual(test["interpretation"]["version"], 1)
        self.assertEqual(test["interpretation"]["submitted_by_person"]["role"],
                         "geology")

    def test_trace_shows_blockers_after_maintenance(self) -> None:
        self.hub.operations.create_window(
            self.actor("maint"), self.well["id"],
            self.future(3).isoformat(), self.future(4).isoformat(), "检修")
        t = self.hub.trace.trace_gas_day(
            self.actor("disp"), self.future(3).isoformat())
        commitment = t["commitments"][0]
        blockers = commitment["arrangements"][0]["active_blockers"]
        self.assertTrue(any(b["id"].startswith("MNT-") for b in blockers))
        well_day = self.hub.trace.trace_well_day(
            self.well["id"], self.future(3).isoformat())
        self.assertTrue(well_day["maintenance"])

    def test_commercial_trace_scoped_to_own_contract(self) -> None:
        t = self.hub.trace.trace_gas_day(
            self.actor("buyer"), self.future(3).isoformat())
        self.assertEqual(
            [c["commitment"]["id"] for c in t["commitments"]],
            [self.commitment["id"]])
        # 快照中的测算信息只含本合同交付通道
        snapshot = t["commitments"][0]["decisions"][0]["snapshot"]
        self.assertEqual(
            {c["channel_id"] for c in snapshot["projection"]["channels"]},
            {self.channel["id"]})
        self.assertTrue(snapshot["projection"]["wells"])
        # 不能追溯别人的承诺
        uid2, _ = self.hub.auth.create_user("乙客户", "commercial")
        c2 = self.hub.auth.create_contract("乙合同", uid2, self.channel["id"])
        other = self.hub.commitments.create_commitment(
            self.actor("disp"), c2, self.future(3).isoformat(), 5.0)
        from src.dgpc.errors import PermissionError

        with self.assertRaises(PermissionError):
            self.hub.trace.trace_commitment(self.actor("buyer"), other["id"])
