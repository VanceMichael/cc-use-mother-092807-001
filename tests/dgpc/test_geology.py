"""储层解释版本与测试批次复核。"""

from src.dgpc.errors import ConflictError, PermissionError
from tests.dgpc.helpers import HubCase


class GeologyTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.well = self.make_block_well()

    def test_interpretation_versions_only_increment(self) -> None:
        i1 = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        i2 = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 2})
        self.assertEqual((i1["version"], i2["version"]), (1, 2))
        rows = self.hub.geology.list_interpretations(self.well["id"])
        self.assertEqual([r["version"] for r in rows], [2, 1])

    def test_test_batch_starts_pending_and_survives(self) -> None:
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        batch = self.hub.geology.submit_test_batch(
            self.actor("rig"), self.well["id"], 80.0,
            self.past(1).isoformat(), "放喷测试6小时", "medium", interp["id"])
        self.assertEqual(batch["status"], "pending_review")
        pending = self.hub.geology.list_test_batches("pending_review")
        self.assertEqual([b["id"] for b in pending], [batch["id"]])

    def test_submitter_cannot_review_own_batch(self) -> None:
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        batch = self.hub.geology.submit_test_batch(
            self.actor("geo"), self.well["id"], 80.0,
            self.past(1).isoformat(), "放喷测试", "medium", interp["id"])
        with self.assertRaises(PermissionError):
            self.hub.geology.review_test_batch(
                self.actor("geo"), batch["id"], True)

    def test_other_dispatcher_can_review_and_only_once(self) -> None:
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        batch = self.hub.geology.submit_test_batch(
            self.actor("geo"), self.well["id"], 80.0,
            self.past(1).isoformat(), "放喷测试", "medium", interp["id"])
        self.hub.geology.review_test_batch(
            self.actor("disp"), batch["id"], True, "同意")
        with self.assertRaises(ConflictError):
            self.hub.geology.review_test_batch(
                self.actor("disp"), batch["id"], False)

    def test_rejected_batch_cannot_publish_offer(self) -> None:
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        batch = self.hub.geology.submit_test_batch(
            self.actor("rig"), self.well["id"], 80.0,
            self.past(1).isoformat(), "放喷测试", "medium", interp["id"])
        self.hub.geology.review_test_batch(
            self.actor("disp"), batch["id"], False, "制度不稳")
        with self.assertRaises(ConflictError):
            self.hub.capacity.publish_from_test(
                self.actor("disp"), batch["id"],
                self.past(1).isoformat(), self.future(30).isoformat())

    def test_batch_with_quarantined_material_rejected(self) -> None:
        m = self.hub.materials.receive(
            self.actor("rig"), "RC-1", "production_receipt",
            {"v": 1}, self.well["id"])
        self.hub.materials.receive(
            self.actor("rig"), "RC-1", "production_receipt",
            {"v": 2}, self.well["id"])
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), self.well["id"], "栖霞组", {"p": 1})
        from src.dgpc.errors import QuarantineError

        with self.assertRaises(QuarantineError):
            self.hub.geology.submit_test_batch(
                self.actor("rig"), self.well["id"], 80.0,
                self.past(1).isoformat(), "测试", "medium", interp["id"], m["id"])
