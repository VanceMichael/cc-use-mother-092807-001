"""系统重启后值班队列继续存在。"""

import os
import tempfile

from src.dgpc.db import connect, init_db
from src.dgpc.server import build_hub
from tests.dgpc.helpers import HubCase


class RecoveryPersistenceTest(HubCase):
    def test_pending_review_windows_gaps_survive_restart(self) -> None:
        _, well = self.make_block_well()
        channel = self.make_channel(capacity=50.0)
        # 待复核测试
        interp = self.hub.geology.submit_interpretation(
            self.actor("geo"), well["id"], "栖霞组", {"p": 1})
        self.hub.geology.submit_test_batch(
            self.actor("geo"), well["id"], 80.0, self.past(1).isoformat(),
            "待复核测试", "medium", interp["id"])
        # 过期待解锁检修
        self.hub.operations.create_window(
            self.actor("maint"), well["id"],
            self.past(3).isoformat(), self.past(1).isoformat(), "检修")
        # 缺口：合同承诺 90，通道可靠产能为 0（井在检修）
        contract = self.hub.auth.create_contract(
            "甲合同", self.u["buyer"], channel["id"])
        self.hub.commitments.create_commitment(
            self.actor("disp"), contract,
            self.future(2).isoformat(), 90.0)

        path = tempfile.mktemp(suffix=".sqlite3")
        try:
            self.conn.backup(self._open_file_conn(path))
            hub2 = build_hub(path)
            rec = hub2.recover()
            self.assertEqual(len(rec["pending_test_reviews"]), 1)
            self.assertEqual(len(rec["await_unlock_windows"]), 1)
            self.assertEqual(len(rec["open_gap_reminders"]), 1)
            gap = rec["open_gap_reminders"][0]
            self.assertAlmostEqual(gap["shortage"], 90.0, places=3)
            hub2.conn.close()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def _open_file_conn(self, path: str):
        conn = connect(path)
        init_db(conn)
        return conn
