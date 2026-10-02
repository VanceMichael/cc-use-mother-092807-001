"""回执幂等与同号异值隔离。"""

from src.deep_gas import inbound
from src.deep_gas.errors import AuthorizationError, Quarantined

from domain_fixture import DomainFixture


class InboundTest(DomainFixture):
    def test_duplicate_receipt_is_not_accumulated(self) -> None:
        payload = {"well": "W2", "flow_rate": 50.0}
        first = inbound.ingest(self.conn, self.actors["drill"], "R1", "well_log", payload)
        second = inbound.ingest(self.conn, self.actors["drill"], "R1", "well_log",
                                {"flow_rate": 50.0, "well": "W2"})  # 键序不同但值相同
        self.assertEqual(first["outcome"], "accepted")
        self.assertEqual(second["outcome"], "duplicate")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"], 1)

    def test_same_number_different_value_is_quarantined(self) -> None:
        inbound.ingest(self.conn, self.actors["drill"], "R2", "well_log", {"v": 1})
        with self.assertRaises(Quarantined):
            inbound.ingest(self.conn, self.actors["drill"], "R2", "well_log", {"v": 2})
        # 台账仍只有一份，冲突件进入隔离区
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM receipts WHERE receipt_no='R2'").fetchone()["n"], 1)
        q = inbound.list_quarantine(self.conn, self.actors["disp"])
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["receipt_no"], "R2")

    def test_quarantine_resolution_accept_then_discard(self) -> None:
        inbound.ingest(self.conn, self.actors["drill"], "R3", "meter_reading", {"v": 1})
        with self.assertRaises(Quarantined):
            inbound.ingest(self.conn, self.actors["drill"], "R3", "meter_reading", {"v": 9})
        qid = inbound.list_quarantine(self.conn, self.actors["disp"])[0]["id"]
        inbound.resolve_quarantine(self.conn, self.actors["disp"], qid, "discard")
        # 丢弃后不再出现在待裁决列表
        self.assertEqual(inbound.list_quarantine(self.conn, self.actors["disp"]), [])

    def test_commercial_cannot_ingest(self) -> None:
        with self.assertRaises(AuthorizationError):
            inbound.ingest(self.conn, self.actors["buyer_a"], "R4", "well_log", {"v": 1})
