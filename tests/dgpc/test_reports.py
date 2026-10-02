"""日报定稿与更正版本链。"""

from src.dgpc.errors import ConflictError, NotFoundError
from tests.dgpc.helpers import HubCase


class ReportTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        _, self.well = self.make_block_well()

    def test_report_finalized_and_no_duplicate(self) -> None:
        r = self.hub.reports.submit_report(
            self.actor("rig"), self.well["id"],
            self.past(1).isoformat(), 41.2)
        self.assertEqual(r["status"], "finalized")
        self.assertEqual(r["version"], 1)
        with self.assertRaises(ConflictError):
            self.hub.reports.submit_report(
                self.actor("rig"), self.well["id"],
                self.past(1).isoformat(), 41.5)

    def test_correction_chain_preserves_history(self) -> None:
        r1 = self.hub.reports.submit_report(
            self.actor("rig"), self.well["id"],
            self.past(1).isoformat(), 41.2)
        r2 = self.hub.reports.correct_report(
            self.actor("disp"), self.well["id"],
            self.past(1).isoformat(), 39.8, "计量器具复检后修正")
        self.assertEqual(r2["version"], 2)
        self.assertEqual(r2["corrects_id"], r1["id"])
        self.assertEqual(
            self.hub.reports.get_report(r1["id"])["status"], "corrected")
        chain = self.hub.reports.report_chain(
            self.well["id"], self.past(1).isoformat())
        self.assertEqual([x["version"] for x in chain], [1, 2])
        self.assertEqual([x["rate"] for x in chain], [41.2, 39.8])
        latest = self.hub.reports.latest_report(
            self.well["id"], self.past(1).isoformat())
        self.assertEqual(latest["rate"], 39.8)

    def test_correction_requires_note_and_existing_report(self) -> None:
        from src.dgpc.errors import ValidationError

        with self.assertRaises(NotFoundError):
            self.hub.reports.correct_report(
                self.actor("disp"), self.well["id"],
                self.past(2).isoformat(), 39.8, "尚无日报不能更正")
        self.hub.reports.submit_report(
            self.actor("rig"), self.well["id"],
            self.past(1).isoformat(), 41.2)
        with self.assertRaises(ValidationError):
            self.hub.reports.correct_report(
                self.actor("disp"), self.well["id"],
                self.past(1).isoformat(), 40.0, "  ")
