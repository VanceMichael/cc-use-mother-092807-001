"""产能声明：时间窗、置信依据、口径更替不重复、例外职责分离。"""

from src.dgpc.errors import ConflictError, PermissionError, ValidationError
from tests.dgpc.helpers import HubCase


class CapacityTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.well = self.make_block_well()
        self.channel = self.make_channel()

    def test_offer_requires_window_and_basis_via_test(self) -> None:
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        batch = self.hub.geology.submit_test_batch(
            self.actor("geo"), self.well["id"], 90.0,
            self.past(1).isoformat(), "放喷6小时油压稳定", "high", interp["id"])
        with self.assertRaises(ConflictError):
            # 未复核测试不能登记产能
            self.hub.capacity.publish_from_test(
                self.actor("disp"), batch["id"],
                self.past(1).isoformat(), self.future(30).isoformat())
        self.hub.geology.review_test_batch(
            self.actor("disp"), batch["id"], True)
        with self.assertRaises(ValidationError):
            self.hub.capacity.publish_from_test(
                self.actor("disp"), batch["id"],
                self.future(30).isoformat(), self.past(1).isoformat())
        offer = self.hub.capacity.publish_from_test(
            self.actor("disp"), batch["id"],
            self.past(1).isoformat(), self.future(30).isoformat())
        self.assertEqual(offer["stage"], "tested")
        self.assertEqual(offer["rate"], 90.0)
        self.assertIn("TST-", offer["basis"])

    def test_promotion_replaces_not_adds(self) -> None:
        _, offer = self.make_approved_offer(self.well["id"], 90.0)
        active = [o for o in self.hub.capacity.list_offers("active")]
        self.assertEqual(len(active), 1)
        stable = self.hub.capacity.promote(
            self.actor("disp"), offer["id"],
            self.future(1).isoformat(), self.future(120).isoformat(),
            85.0, "high", "连续30日试采82~87，转稳产")
        # 旧口径被更替
        self.assertEqual(self.hub.capacity.get_offer(offer["id"])["status"],
                         "superseded")
        active = self.hub.capacity.list_offers("active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], stable["id"])
        self.assertEqual(active[0]["stage"], "trial")
        # 再转正到稳产
        st2 = self.hub.capacity.promote(
            self.actor("disp"), stable["id"],
            self.future(31).isoformat(), self.future(200).isoformat(),
            84.0, "high", "继续稳产")
        self.assertEqual(self.hub.capacity.get_offer(stable["id"])["status"],
                         "superseded")
        self.assertEqual(len(self.hub.capacity.list_offers("active")), 1)
        # 同一旧产能不能重复转正
        with self.assertRaises(ConflictError):
            self.hub.capacity.promote(
                self.actor("disp"), offer["id"],
                self.future(60).isoformat(), self.future(180).isoformat(),
                80.0, "high", "重复转正")

    def test_same_batch_cannot_publish_twice(self) -> None:
        batch, offer = self.make_approved_offer(self.well["id"], 90.0)
        with self.assertRaises(ConflictError):
            self.hub.capacity.publish_from_test(
                self.actor("disp"), batch["id"],
                self.past(1).isoformat(), self.future(30).isoformat())
        self.assertEqual(len(self.hub.capacity.list_offers("active")), 1)

    def test_exception_separation_of_duties(self) -> None:
        exc = self.hub.capacity.submit_exception(
            self.actor("geo"), self.well["id"], 70.0,
            self.today.isoformat(), self.future(20).isoformat(),
            "medium", "邻井压裂见效，临时上调配产")
        # 地质提交人本人不能审批
        with self.assertRaises(PermissionError):
            self.hub.capacity.review_exception(
                self.actor("geo"), exc["id"], True)
        # 其他角色（如井队）无权审批
        from src.dgpc.errors import PermissionError as PErr

        # 调度审批通过，生成唯一 active 产能
        result = self.hub.capacity.review_exception(
            self.actor("disp"), exc["id"], True, "同意临时调配")
        self.assertEqual(result["offer_id"][:4], "CAP-")
        active = self.hub.capacity.list_offers("active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["stage"], "exception")
        # 同一例外不能重复生成产能
        with self.assertRaises(ConflictError):
            self.hub.capacity.review_exception(
                self.actor("disp"), exc["id"], True)
        # rig 角色无权
        with self.assertRaises(PErr):
            self.hub.auth.require(self.actor("rig"), "exception:review")

    def test_exception_reject_does_not_create_offer(self) -> None:
        exc = self.hub.capacity.submit_exception(
            self.actor("geo"), self.well["id"], 70.0,
            self.today.isoformat(), self.future(20).isoformat(),
            "medium", "理由")
        self.hub.capacity.review_exception(
            self.actor("disp"), exc["id"], False, "依据不足")
        self.assertEqual(len(self.hub.capacity.list_offers("active")), 0)

    def test_exception_on_well_with_active_offer_supersedes(self) -> None:
        _, offer = self.make_approved_offer(self.well["id"], 50.0)
        exc = self.hub.capacity.submit_exception(
            self.actor("geo"), self.well["id"], 60.0,
            self.today.isoformat(), self.future(10).isoformat(),
            "medium", "压裂调整临时增产")
        result = self.hub.capacity.review_exception(
            self.actor("disp"), exc["id"], True)
        self.assertEqual(
            self.hub.capacity.get_offer(offer["id"])["status"], "superseded")
        active = self.hub.capacity.list_offers("active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], result["offer_id"])

    def test_low_confidence_excluded_from_reliable(self) -> None:
        _, offer = self.make_approved_offer(
            self.well["id"], 50.0, confidence="low")
        self.hub.catalog.attach_route(self.well["id"], self.channel["id"])
        proj = self.hub.projection.reliable_supply(self.today)
        w = next(x for x in proj["wells"] if x["well_id"] == self.well["id"])
        self.assertEqual(w["firm_rate"], 0.0)
        self.assertIn("low", " ".join(w["reasons"]))
