"""供气承诺：通道能力分配、缺口提醒、事件只改未来安排。"""

from src.dgpc.errors import ConflictError, PermissionError
from tests.dgpc.helpers import HubCase


class CommitmentTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        self.b1, self.w1 = self.make_block_well("西1", "西区")
        self.b2, self.w2 = self.make_block_well("西2", "西区")
        self.channel = self.make_channel("西干线", 100.0)
        self.make_approved_offer(self.w1["id"], 60.0, channel=self.channel)
        self.make_approved_offer(self.w2["id"], 40.0, channel=self.channel)
        self.contract = self.hub.auth.create_contract(
            "甲客户合同", self.u["buyer"], self.channel["id"])
        self.buyer = self.actor("buyer")
        self.disp = self.actor("disp")

    def _commitment(self, day_offset: int, volume: float):
        return self.hub.commitments.create_commitment(
            self.disp, self.contract,
            self.future(day_offset).isoformat(), volume, "测试承诺")

    def test_commitment_creates_decision_and_arrangements(self) -> None:
        c = self._commitment(2, 80.0)
        self.assertEqual(len(c["decisions"]), 1)
        self.assertEqual(c["decisions"][0]["version"], 1)
        total = sum(a["planned_rate"] for a in c["arrangements"])
        self.assertAlmostEqual(total, 80.0, places=3)

    def test_overcommit_opens_gap_and_recovery_resolves(self) -> None:
        c = self._commitment(2, 150.0)  # 当前两井合计只有 100
        gap = c["open_gap"]
        self.assertIsNotNone(gap)
        self.assertAlmostEqual(gap["shortage"], 50.0, places=3)
        open_gaps = self.hub.commitments.list_open_gaps()
        self.assertEqual([g["id"] for g in open_gaps], [gap["id"]])
        # 供应恢复：通道扩容到 200，且同通道新井投产 50；
        # 产能发布事件自动重算未来安排
        self.conn.execute(
            "update export_channels set capacity=200 where id=?",
            (self.channel["id"],))
        _, w3 = self.make_block_well("西3", "西区")
        self.make_approved_offer(w3["id"], 50.0, channel=self.channel)
        c2 = self.hub.commitments.get_commitment(self.disp, c["id"])
        self.assertIsNone(c2["open_gap"])
        self.assertGreaterEqual(c2["decisions"][-1]["version"], 2)

    def test_two_commitments_same_channel_share_pool(self) -> None:
        c1 = self._commitment(2, 80.0)
        # 第二份合同/承诺同日，剩余能力只有 20
        uid2, _ = self.hub.auth.create_user("乙客户", "commercial")
        contract2 = self.hub.auth.create_contract(
            "乙客户合同", uid2, self.channel["id"])
        c2 = self.hub.commitments.create_commitment(
            self.disp, contract2, self.future(2).isoformat(), 50.0)
        total2 = sum(a["planned_rate"] for a in c2["arrangements"])
        self.assertAlmostEqual(total2, 20.0, places=3)
        self.assertAlmostEqual(c2["open_gap"]["shortage"], 30.0, places=3)
        # 两份承诺合计不超过通道能力
        a1 = sum(a["planned_rate"] for a in c1["arrangements"])
        self.assertAlmostEqual(a1 + total2, 100.0, places=3)

    def test_maintenance_event_replans_future_only(self) -> None:
        c = self._commitment(5, 80.0)
        well_ids = {a["well_id"] for a in c["arrangements"]}
        self.assertIn(self.w2["id"], well_ids)
        # 第 5 天检修 w2（事件自动触发重规划）
        win = self.hub.operations.create_window(
            self.actor("maint"), self.w2["id"],
            self.future(5).isoformat(), self.future(6).isoformat(), "压缩机检修")
        c = self.hub.commitments.get_commitment(self.disp, c["id"])
        versions = [d["version"] for d in c["decisions"]]
        self.assertIn(2, versions)
        w2_arrangements = [
            a for a in c["arrangements"] if a["well_id"] == self.w2["id"]]
        self.assertTrue(all(a["status"] == "cancelled" for a in w2_arrangements))
        self.assertIsNotNone(c["open_gap"])
        # 解锁后自动恢复，缺口消除，决定到 v3
        self.hub.operations.unlock(self.actor("maint"), win["id"])
        c = self.hub.commitments.get_commitment(self.disp, c["id"])
        self.assertIsNone(c["open_gap"])
        self.assertEqual(c["decisions"][-1]["version"], 3)

    def test_today_arrangement_frozen_after_report(self) -> None:
        c = self.hub.commitments.create_commitment(
            self.disp, self.contract, self.today.isoformat(), 80.0)
        # 日报定稿，安排转 fulfilled
        self.hub.reports.submit_report(
            self.actor("rig"), self.w1["id"], self.today.isoformat(), 55.0)
        c = self.hub.commitments.get_commitment(self.disp, c["id"])
        a = next(x for x in c["arrangements"] if x["well_id"] == self.w1["id"])
        self.assertEqual(a["status"], "fulfilled")
        self.assertEqual(a["actual_rate"], 55.0)
        # 随后的停井事件不得改动已履行安排
        self.hub.operations.create_window(
            self.actor("maint"), self.w1["id"],
            self.today.isoformat(), self.future(1).isoformat(), "突发检修")
        c = self.hub.commitments.get_commitment(self.disp, c["id"])
        a = next(x for x in c["arrangements"] if x["well_id"] == self.w1["id"])
        self.assertEqual(a["status"], "fulfilled")
        self.assertEqual(a["actual_rate"], 55.0)

    def test_past_commitment_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.hub.commitments.create_commitment(
                self.disp, self.contract, self.past(1).isoformat(), 10.0)

    def test_duplicate_commitment_same_contract_day_rejected(self) -> None:
        self._commitment(2, 10.0)
        with self.assertRaises(ConflictError):
            self._commitment(2, 10.0)

    def test_commercial_visibility(self) -> None:
        own = self._commitment(2, 10.0)
        # 客户能看到自己的承诺
        visible = self.hub.commitments.list_commitments(self.buyer)
        self.assertEqual([c["id"] for c in visible], [own["id"]])
        detail = self.hub.commitments.get_commitment(
            self.buyer, own["id"])
        self.assertEqual(detail["id"], own["id"])
        # 别人的承诺不可见
        uid2, _ = self.hub.auth.create_user("乙客户", "commercial")
        contract2 = self.hub.auth.create_contract(
            "乙合同", uid2, self.channel["id"])
        other = self.hub.commitments.create_commitment(
            self.disp, contract2, self.future(2).isoformat(), 5.0)
        with self.assertRaises(PermissionError):
            self.hub.commitments.get_commitment(self.buyer, other["id"])
