"""测试批次到产能谱系：待复核、口径晋级不重复累计、储层版本递增。"""

from src.deep_gas import catalog, testing
from src.deep_gas.errors import Conflict

from domain_fixture import DomainFixture


class CapacityLineageTest(DomainFixture):
    def _reviewed_capacity(self) -> str:
        testing.register_test(self.conn, self.actors["drill"], "T1", "W1",
                              "2026-09-20", 100.0)
        testing.review_test(self.conn, self.actors["disp"], "T1", True)
        return testing.convert_to_capacity(
            self.conn, self.actors["disp"], "T1", "2026-09-25")["claim_id"]

    def test_pending_test_survives_and_blocks_conversion(self) -> None:
        testing.register_test(self.conn, self.actors["drill"], "T0", "W1",
                              "2026-09-19", 90.0)
        pending = testing.pending_tests(self.conn, self.actors["disp"])
        self.assertEqual([t["batch_no"] for t in pending], ["T0"])
        with self.assertRaises(Conflict):
            testing.convert_to_capacity(self.conn, self.actors["disp"], "T0", "2026-09-25")

    def test_promotion_is_single_stepped_and_non_overlapping(self) -> None:
        claim_id = self._reviewed_capacity()
        with self.assertRaises(Conflict):  # testing 不能直接跳到 stable
            testing.promote_capacity(self.conn, self.actors["disp"], claim_id,
                                     "stable", "2026-10-01", rate=95.0, confidence="high")
        testing.promote_capacity(self.conn, self.actors["disp"], claim_id, "trial",
                                 "2026-10-01", rate=95.0, confidence="medium")
        testing.promote_capacity(self.conn, self.actors["disp"], claim_id, "stable",
                                 "2026-10-08", rate=90.0, confidence="high")
        # 任一供气日恰好命中一个版本
        v_sep = testing.effective_claim_version(self.conn, claim_id, "2026-09-30")
        v_oct5 = testing.effective_claim_version(self.conn, claim_id, "2026-10-05")
        v_oct9 = testing.effective_claim_version(self.conn, claim_id, "2026-10-09")
        self.assertEqual(v_sep["basis_kind"], "testing")
        self.assertEqual(v_oct5["basis_kind"], "trial")
        self.assertEqual(v_oct9["basis_kind"], "stable")
        # 谱系总产能声明在任何供气日只算一次（各版本时间窗不重叠）
        rows = self.conn.execute(
            "SELECT version_no, valid_from, valid_to FROM capacity_claim_versions "
            "WHERE claim_id=? ORDER BY version_no", (claim_id,)).fetchall()
        self.assertEqual([r["valid_to"] for r in rows[:-1]], ["2026-09-30", "2026-10-07"])
        self.assertIsNone(rows[-1]["valid_to"])

    def test_test_batch_converts_only_once(self) -> None:
        self._reviewed_capacity()
        with self.assertRaises(Conflict):
            testing.convert_to_capacity(self.conn, self.actors["disp"], "T1", "2026-10-01")

    def test_rejected_test_cannot_become_capacity(self) -> None:
        testing.register_test(self.conn, self.actors["drill"], "TJ", "W2",
                              "2026-09-20", 10.0)
        testing.review_test(self.conn, self.actors["disp"], "TJ", False)
        with self.assertRaises(Conflict):
            testing.convert_to_capacity(self.conn, self.actors["disp"], "TJ", "2026-09-25")

    def test_reservoir_versions_are_monotonic(self) -> None:
        well = catalog.get_well(self.conn, "W1")
        a = catalog.submit_reservoir_version(self.conn, self.actors["geo"], "W1", "F1", "解释一")
        b = catalog.submit_reservoir_version(self.conn, self.actors["geo"], "W1", "F1", "解释二")
        self.assertEqual((a["version_no"], b["version_no"]), (1, 2))
        latest = catalog.latest_reservoir(self.conn, well["id"], "F1")
        self.assertEqual(latest["interpretation"], "解释二")
