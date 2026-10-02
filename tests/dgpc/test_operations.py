"""作业、检修、限输对可靠供应的影响，以及只改未来安排的规则。"""

from src.dgpc.errors import ConflictError
from tests.dgpc.helpers import HubCase


class OperationsProjectionTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.well = self.make_block_well()
        self.channel = self.make_channel(capacity=100.0)
        self.batch, self.offer = self.make_approved_offer(
            self.well["id"], 50.0, channel=self.channel)

    def test_maintenance_window_blocks_supply_until_unlock(self) -> None:
        # 检修覆盖明天到后天
        self.hub.operations.create_window(
            self.actor("maint"), self.well["id"],
            self.future(1).isoformat(), self.future(2).isoformat(), "压缩机检修")
        proj = self.hub.projection.reliable_supply(self.future(1))
        w = next(x for x in proj["wells"] if x["well_id"] == self.well["id"])
        self.assertEqual(w["firm_rate"], 0.0)
        self.assertTrue(any(b["type"] == "maintenance" for b in w["blockers"]))
        # 解锁后恢复
        win = self.hub.operations.list_windows()[0]
        self.hub.operations.unlock(self.actor("maint"), win["id"])
        proj = self.hub.projection.reliable_supply(self.future(1))
        w = next(x for x in proj["wells"] if x["well_id"] == self.well["id"])
        self.assertEqual(w["firm_rate"], 50.0)

    def test_overdue_window_keeps_blocking_and_enters_queue(self) -> None:
        self.hub.operations.create_window(
            self.actor("maint"), self.well["id"],
            self.past(3).isoformat(), self.past(1).isoformat(), "检修延期")
        rec = self.hub.recover(self.today)
        self.assertEqual(len(rec["await_unlock_windows"]), 1)
        proj = self.hub.projection.reliable_supply(self.today)
        w = next(x for x in proj["wells"] if x["well_id"] == self.well["id"])
        self.assertEqual(w["firm_rate"], 0.0)

    def test_work_program_schedule_reschedule_and_complete(self) -> None:
        prog = self.hub.operations.create_program(
            self.actor("rig"), self.well["id"], "acidizing",
            self.future(5).isoformat(), self.future(7).isoformat(), "酸化解堵")
        # 未开始可改期并留痕
        self.hub.operations.reschedule_program(
            self.actor("rig"), prog["id"],
            self.future(8).isoformat(), self.future(9).isoformat(), "物资推迟")
        row = self.hub.operations.get_program(prog["id"])
        self.assertEqual(row["planned_start"], self.future(8).isoformat())
        # 开工
        self.hub.operations.start_program(self.actor("rig"), prog["id"])
        with self.assertRaises(ConflictError):
            self.hub.operations.reschedule_program(
                self.actor("rig"), prog["id"],
                self.future(10).isoformat(), self.future(11).isoformat(), "再改")
        # 完工后旧产能失效，等待复产测试
        self.hub.operations.complete_program(self.actor("rig"), prog["id"])
        self.assertEqual(
            self.hub.capacity.get_offer(self.offer["id"])["status"], "superseded")
        proj = self.hub.projection.reliable_supply(self.today)
        w = next(x for x in proj["wells"] if x["well_id"] == self.well["id"])
        self.assertEqual(w["firm_rate"], 0.0)

    def test_reopen_after_workover_via_new_test_not_double_count(self) -> None:
        prog = self.hub.operations.create_program(
            self.actor("rig"), self.well["id"], "workover",
            self.past(3).isoformat(), self.past(1).isoformat(), "大修")
        self.hub.operations.start_program(self.actor("rig"), prog["id"])
        self.hub.operations.complete_program(self.actor("rig"), prog["id"])
        # 复产新测试与新解释版本
        interp2 = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 9})
        batch2 = self.hub.geology.submit_test_batch(
            self.actor("rig"), self.well["id"], 62.0,
            self.past(1).isoformat(), "大修后复产测试", "high", interp2["id"])
        self.hub.geology.review_test_batch(
            self.actor("disp"), batch2["id"], True)
        offer2 = self.hub.capacity.publish_from_test(
            self.actor("disp"), batch2["id"],
            self.past(1).isoformat(), self.future(90).isoformat())
        active = self.hub.capacity.list_offers("active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], offer2["id"])
        self.assertEqual(active[0]["rate"], 62.0)
        proj = self.hub.projection.reliable_supply(self.today)
        w = next(x for x in proj["wells"] if x["well_id"] == self.well["id"])
        self.assertEqual(w["firm_rate"], 62.0)

    def test_curtailment_scales_channel_throughput(self) -> None:
        _, other = self.make_block_well("邻井", "区块二")
        self.hub.catalog.attach_route(other["id"], self.channel["id"])
        self.make_approved_offer(other["id"], 80.0, channel=self.channel)
        # 两井合计 130 > 通道 100，无限输时按比例到 100
        proj = self.hub.projection.reliable_supply(self.today)
        self.assertAlmostEqual(proj["firm_total"], 100.0, places=3)
        # 限输到 65
        cur = self.hub.operations.create_curtailment(
            self.actor("pipe"), self.channel["id"], 65.0,
            self.future(1).isoformat(), self.future(3).isoformat(), "连头")
        proj = self.hub.projection.reliable_supply(self.future(2))
        self.assertAlmostEqual(proj["firm_total"], 65.0, places=3)
        # 解除后恢复
        self.hub.operations.lift_curtailment(self.actor("pipe"), cur["id"])
        proj = self.hub.projection.reliable_supply(self.future(2))
        self.assertAlmostEqual(proj["firm_total"], 100.0, places=3)

    def test_future_curtailment_does_not_change_today(self) -> None:
        self.hub.operations.create_curtailment(
            self.actor("pipe"), self.channel["id"], 10.0,
            self.future(5).isoformat(), self.future(6).isoformat(), "未来限输")
        proj = self.hub.projection.reliable_supply(self.today)
        self.assertAlmostEqual(proj["firm_total"], 50.0, places=3)

    def test_reschedule_started_curtailment_rejected(self) -> None:
        cur = self.hub.operations.create_curtailment(
            self.actor("pipe"), self.channel["id"], 10.0,
            self.past(1).isoformat(), self.future(1).isoformat(), "生效中")
        with self.assertRaises(ConflictError):
            self.hub.operations.reschedule_curtailment(
                self.actor("pipe"), cur["id"],
                self.future(10).isoformat(), self.future(11).isoformat(), "改期")
