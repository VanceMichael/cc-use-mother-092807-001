"""服务编排中心：组装各领域服务、事件钩子与重启恢复。

系统重启后：
1. 检修窗口状态按当日重新推进（过了解锁日的 -> await_unlock 待解锁）；
2. 待复核测试批次直接从表中读出，继续等待复核；
3. 未消除的承诺缺口提醒继续保持 open；
4. 供应类事件发生时自动重算尚未履行的供气安排。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .auth import AuthService
from .capacity import CapacityService
from .catalog import CatalogService
from .commitments import CommitmentService
from .geology import GeologyService
from .materials import MaterialService
from .operations import OperationsService
from .projection import ProjectionService
from .reports import ReportService
from .trace import TraceService


class ServiceHub:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.auth = AuthService(conn)
        self.catalog = CatalogService(conn)
        self.materials = MaterialService(conn)
        self.geology = GeologyService(conn)
        self.capacity = CapacityService(conn)
        self.operations = OperationsService(conn)
        self.projection = ProjectionService(conn, self.operations)
        self.commitments = CommitmentService(conn, self.projection)
        self.reports = ReportService(conn)
        self.trace = TraceService(conn)

        # 事件钩子：作业/检修/限输/产能口径/通道接入变化 -> 重算未来供气安排
        self.operations.supply_changed = self._on_supply_changed
        self.capacity.supply_changed = self._on_supply_changed
        self.catalog.supply_changed = self._on_supply_changed
        # 日报定稿 -> 安排转已履行
        self.reports.after_finalize = self.commitments.mark_arrangement_actuals

    # -- 事件处理 -------------------------------------------------------------
    def _on_supply_changed(self, actor: dict, reason: str) -> None:
        self.commitments.replan_future(actor, reason)

    # -- 启动恢复 -------------------------------------------------------------
    def recover(self, today: date | None = None) -> dict:
        """重启后调用：恢复待复核/待解锁/缺口等值班队列。"""
        today = today or date.today()
        self.operations.refresh_curtailment_states(today)
        due_windows = self.operations.refresh_window_states(today)
        return {
            "recovered_at": today.isoformat(),
            "pending_test_reviews": [
                dict(r)
                for r in self.conn.execute(
                    "select id,well_id,submitted_by,submitted_at,flow_rate,"
                    "test_date,confidence from test_batches "
                    "where status='pending_review' order by submitted_at, id"
                )
            ],
            "await_unlock_windows": [
                dict(r)
                for r in self.conn.execute(
                    "select id,well_id,lock_from,unlock_due,created_by "
                    "from maintenance_windows where status='await_unlock' "
                    "order by unlock_due, id"
                )
            ],
            "newly_due_windows": due_windows,
            "open_gap_reminders": self.commitments.list_open_gaps(),
            "quarantined_materials": self.materials.list_quarantined(),
            "pending_exceptions": self.capacity.list_exceptions("pending"),
        }

    # -- 值班队列（运行期查询同样走这里）--------------------------------------
    def duty_queue(self, today: date | None = None) -> dict:
        today = today or date.today()
        self.operations.refresh_window_states(today)
        self.operations.refresh_curtailment_states(today)
        return {
            "as_of": today.isoformat(),
            "pending_test_reviews": self.geology.list_test_batches(
                "pending_review"
            ),
            "await_unlock_windows": self.operations.list_windows("await_unlock"),
            "open_gap_reminders": self.commitments.list_open_gaps(),
            "quarantined_materials": self.materials.list_quarantined(),
            "pending_exceptions": self.capacity.list_exceptions("pending"),
        }
