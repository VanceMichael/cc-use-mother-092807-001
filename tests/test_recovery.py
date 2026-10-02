"""系统重启恢复：关闭进程后重新打开同一文件库，待办与决定全部保留。"""

import tempfile
import unittest
from pathlib import Path

from src.deep_gas import auth, catalog, inbound, operations, supply, testing
from src.deep_gas.db import connect
from src.deep_gas.errors import Quarantined
from src.deep_gas.recovery import workbench


class RestartRecoveryTest(unittest.TestCase):
    def test_workbench_state_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gas.db"
            conn = connect(db_path)
            auth.register_actor(conn, "disp", "调度员", "dispatch")
            auth.register_actor(conn, "drill", "井队员", "drilling")
            auth.register_actor(conn, "maint", "维护员", "maintenance")
            auth.register_actor(conn, "pipe", "输气员", "pipeline")
            auth.register_actor(conn, "plan", "计划员", "planning")
            disp = auth.get_actor(conn, "disp")
            drill = auth.get_actor(conn, "drill")
            maint = auth.get_actor(conn, "maint")
            pipe = auth.get_actor(conn, "pipe")
            plan = auth.get_actor(conn, "plan")

            catalog.create_block(conn, disp, "B1", "区块")
            catalog.create_well(conn, disp, "B1", "W1", "井一")
            catalog.create_channel(conn, pipe, "C1", "干线", 200.0)
            catalog.add_route(conn, pipe, "W1", "C1")
            catalog.create_contract(conn, plan, "K1", "甲方")
            supply.create_commitment(conn, plan, "K1", "CM1",
                                     100.0, "2026-10-01", "2026-10-31")
            supply.add_commitment_source(conn, plan, "CM1", "W1")

            # 三类待办：待复核测试、检修锁、缺口；另有隔离件
            testing.register_test(conn, drill, "T1", "W1", "2026-10-01", 5.0)
            operations.create_lock(conn, maint, "2026-10-20", "2026-10-21", "阀检",
                                   well_code="W1")
            supply.evaluate_commitment(conn, disp, "CM1", "2026-10-20")  # 锁内可供0 → 缺口
            inbound.ingest(conn, drill, "R1", "well_log", {"v": 1})
            with self.assertRaises(Quarantined):
                inbound.ingest(conn, drill, "R1", "well_log", {"v": 2})
            conn.commit()
            conn.close()

            # —— 模拟进程重启 ——
            conn2 = connect(db_path)
            disp2 = auth.get_actor(conn2, "disp")
            wb = workbench(conn2, disp2)
            self.assertEqual([t["batch_no"] for t in wb["pending_tests"]], ["T1"])
            self.assertEqual(len(wb["maintenance_locks"]), 1)
            self.assertEqual(len(wb["gap_alerts"]), 1)
            self.assertEqual(len(wb["quarantined_materials"]), 1)
            # 历史供气决定可读
            decision = supply.get_decision(conn2, disp2, "CM1", "2026-10-20")
            self.assertEqual(decision["evidence"]["total_available"], 0.0)
            conn2.close()


if __name__ == "__main__":
    unittest.main()
