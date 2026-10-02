"""材料回执：幂等去重、同号异值隔离、处置后才能作为依据。"""

from src.dgpc.errors import ConflictError, QuarantineError
from tests.dgpc.helpers import HubCase


class MaterialTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.well = self.make_block_well()

    def _receive(self, payload, no="WL-1001", kind="well_log", actor="rig"):
        return self.hub.materials.receive(
            self.actor(actor), no, kind, payload, self.well["id"])

    def test_identical_redelivery_is_not_accumulated(self) -> None:
        first = self._receive({"rate": 100, "choke": 12})
        again = self._receive({"rate": 100, "choke": 12})
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["receipt"], "duplicate")
        self.assertEqual(again["status"], "accepted")
        count = self.conn.execute(
            "select count(*) c from inbound_materials").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_same_number_different_value_is_quarantined(self) -> None:
        first = self._receive({"rate": 100})
        conflict = self._receive({"rate": 120})
        self.assertEqual(conflict["id"], first["id"])
        self.assertEqual(conflict["receipt"], "quarantined")
        self.assertEqual(conflict["status"], "quarantined")
        self.assertEqual(len(conflict["variants"]), 1)
        # 首件内容保留，不被覆盖
        self.assertNotEqual(
            conflict["variants"][0]["payload_hash"], first["payload_hash"])
        # 隔离期间同变体再次到达仍是重复，不新增变体
        again = self._receive({"rate": 120})
        self.assertEqual(again["receipt"], "duplicate")
        self.assertEqual(len(self.hub.materials.get_material(first["id"])["variants"]), 1)

    def test_quarantined_material_cannot_back_business(self) -> None:
        first = self._receive({"rate": 100})
        self._receive({"rate": 120})
        with self.assertRaises(QuarantineError):
            self.hub.materials.assert_usable(first["id"])

    def test_resolve_accept_variant_then_usable(self) -> None:
        first = self._receive({"rate": 100})
        conflict = self._receive({"rate": 120})
        variant_id = conflict["variants"][0]["id"]
        resolved = self.hub.materials.resolve(
            self.actor("disp"), first["id"], "accept", "以校核后的120为准",
            variant_id)
        self.assertEqual(resolved["status"], "released")
        self.assertEqual(resolved["chosen_variant_id"], variant_id)
        payload = self.hub.materials.effective_payload(first["id"])
        self.assertEqual(payload["rate"], 120)

    def test_resolve_discard_makes_unusable(self) -> None:
        first = self._receive({"rate": 100})
        self._receive({"rate": 120})
        self.hub.materials.resolve(
            self.actor("disp"), first["id"], "discard", "材料造假，整组丢弃")
        with self.assertRaises(QuarantineError):
            self.hub.materials.assert_usable(first["id"])

    def test_new_value_after_resolution_cannot_reuse_number(self) -> None:
        first = self._receive({"rate": 100})
        self._receive({"rate": 120})
        variant_id = self.hub.materials.get_material(first["id"])["variants"][0]["id"]
        self.hub.materials.resolve(
            self.actor("disp"), first["id"], "accept", variant_id=variant_id)
        with self.assertRaises(ConflictError):
            self._receive({"rate": 130})

    def test_only_dispatch_resolves_quarantine(self) -> None:
        first = self._receive({"rate": 100})
        self._receive({"rate": 120})
        from src.dgpc.errors import PermissionError

        with self.assertRaises(PermissionError):
            self.hub.auth.require(self.actor("rig"), "materials:resolve")
