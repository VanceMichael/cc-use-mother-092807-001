"""作业方案、设备检修窗口与管网限输。

这些事件只改变**尚未履行**的供气安排：
- 计划时间/限输窗在生效前可以调整（留痕）；
- 已开始的作业不能再改期，只能完工或取消后续；
- 已经过去（gas_date <= 今天）的安排与日报一律不动；
- 每次影响供应的事件触发 supply_changed 钩子，由编排层重规划未来安排。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .errors import ConflictError, NotFoundError, ValidationError
from .util import next_id, now_iso, parse_date, positive_rate

PROGRAM_KINDS = frozenset({"workover", "acidizing", "fracturing"})
# 状态：scheduled / in_progress / done / cancelled
BLOCKING_PROGRAM_STATUSES = frozenset({"scheduled", "in_progress"})
BLOCKING_WINDOW_STATUSES = frozenset({"locked", "await_unlock"})


class OperationsService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # 由 ServiceHub 注入：supply_changed(actor, reason) -> None
        self.supply_changed = lambda actor, reason: None

    # == 作业方案 =============================================================
    def create_program(
        self,
        actor: dict,
        well_id: str,
        kind: str,
        planned_start: str,
        planned_end: str,
        note: str = "",
    ) -> dict:
        self._assert_well(well_id)
        if kind not in PROGRAM_KINDS:
            raise ValidationError("作业类型必须是 workover / acidizing / fracturing")
        start = parse_date(planned_start, "作业开始日期")
        end = parse_date(planned_end, "作业结束日期")
        if start > end:
            raise ValidationError("作业开始不能晚于结束")
        pid = next_id(self.conn, "WRK")
        with self.conn:
            self.conn.execute(
                "insert into work_programs(id,well_id,kind,planned_start,planned_end,"
                "status,created_by,created_at,note) values(?,?,?,?,?, 'scheduled',?,?,?)",
                (pid, well_id, kind, start.isoformat(), end.isoformat(),
                 actor["id"], now_iso(), note or None),
            )
        self.supply_changed(actor, f"作业 {pid}（{kind}）已排定")
        return self.get_program(pid)

    def reschedule_program(
        self, actor: dict, program_id: str, new_start: str, new_end: str, reason: str
    ) -> dict:
        prog = self._program_row(program_id)
        if prog["status"] != "scheduled":
            raise ConflictError("只有尚未开始的作业可以改期")
        if date.fromisoformat(prog["planned_start"]) <= date.today():
            raise ConflictError("作业已进入履行期，不能再改动安排；如已完工请登记完工")
        start = parse_date(new_start, "新开始日期")
        end = parse_date(new_end, "新结束日期")
        if start > end:
            raise ValidationError("作业开始不能晚于结束")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("改期必须说明原因")
        with self.conn:
            self.conn.execute(
                "insert into work_program_revisions(id,program_id,old_start,old_end,"
                "new_start,new_end,reason,changed_by,changed_at) "
                "values(?,?,?,?,?,?,?,?,?)",
                (next_id(self.conn, "WRE"), program_id,
                 prog["planned_start"], prog["planned_end"],
                 start.isoformat(), end.isoformat(),
                 reason.strip(), actor["id"], now_iso()),
            )
            self.conn.execute(
                "update work_programs set planned_start=?, planned_end=? where id=?",
                (start.isoformat(), end.isoformat(), program_id),
            )
        self.supply_changed(actor, f"作业 {program_id} 改期：{reason.strip()}")
        return self.get_program(program_id)

    def start_program(self, actor: dict, program_id: str) -> dict:
        prog = self._program_row(program_id)
        if prog["status"] != "scheduled":
            raise ConflictError("只有待开始的作业可以开工")
        with self.conn:
            self.conn.execute(
                "update work_programs set status='in_progress' where id=?",
                (program_id,),
            )
        self.supply_changed(actor, f"作业 {program_id} 已开工")
        return self.get_program(program_id)

    def complete_program(self, actor: dict, program_id: str) -> dict:
        """完工：旧口径产能被更替，井需凭复产测试重新登记产能。"""
        prog = self._program_row(program_id)
        if prog["status"] != "in_progress":
            raise ConflictError("只有进行中的作业可以登记完工")
        with self.conn:
            self.conn.execute(
                "update work_programs set status='done' where id=?", (program_id,)
            )
            # 作业前的产能口径不再代表复产后的井，标记更替以等待复产测试；
            # 不删除记录，保证既往日报与决定仍可追溯
            self.conn.execute(
                "update capacity_offers set status='superseded', "
                "withdraw_reason=coalesce(withdraw_reason,?) "
                "where well_id=? and status='active'",
                (f"作业 {program_id} 完工，待复产测试重新认定", prog["well_id"]),
            )
            self.conn.execute(
                "update wells set status='shut_in' where id=?", (prog["well_id"],)
            )
        self.supply_changed(actor, f"作业 {program_id} 完工，等待复产测试")
        return self.get_program(program_id)

    def cancel_program(self, actor: dict, program_id: str, reason: str) -> dict:
        prog = self._program_row(program_id)
        if prog["status"] == "done":
            raise ConflictError("已完工的作业不能取消")
        if prog["status"] == "in_progress":
            raise ConflictError("进行中的作业请登记完工，不能直接取消")
        if date.fromisoformat(prog["planned_start"]) <= date.today():
            raise ConflictError("已进入履行期的作业不能取消")
        with self.conn:
            self.conn.execute(
                "update work_programs set status='cancelled', note=? where id=?",
                (reason or None, program_id),
            )
        self.supply_changed(actor, f"作业 {program_id} 取消：{reason}")
        return self.get_program(program_id)

    def get_program(self, program_id: str) -> dict:
        return dict(self._program_row(program_id))

    def list_programs(self, well_id: str | None = None) -> list[dict]:
        if well_id:
            rows = self.conn.execute(
                "select * from work_programs where well_id=? order by planned_start",
                (well_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "select * from work_programs order by planned_start"
            ).fetchall()
        return [dict(r) for r in rows]

    # == 设备检修窗口 =========================================================
    def create_window(
        self,
        actor: dict,
        well_id: str,
        lock_from: str,
        unlock_due: str,
        note: str = "",
    ) -> dict:
        self._assert_well(well_id)
        start = parse_date(lock_from, "停井日期")
        due = parse_date(unlock_due, "应解锁日期")
        if start > due:
            raise ValidationError("停井日期不能晚于应解锁日期")
        wid = next_id(self.conn, "MNT")
        with self.conn:
            self.conn.execute(
                "insert into maintenance_windows(id,well_id,lock_from,unlock_due,"
                "status,created_by,created_at,note) values(?,?,?,?, 'locked',?,?,?)",
                (wid, well_id, start.isoformat(), due.isoformat(),
                 actor["id"], now_iso(), note or None),
            )
            self.conn.execute(
                "update wells set status='shut_in' where id=?", (well_id,)
            )
        self.supply_changed(actor, f"检修窗口 {wid} 已登记")
        return self.get_window(wid)

    def reschedule_unlock(
        self, actor: dict, window_id: str, new_due: str, reason: str
    ) -> dict:
        win = self._window_row(window_id)
        if win["status"] not in ("locked", "await_unlock"):
            raise ConflictError("只有停井中的检修窗口可以调整解锁日期")
        due = parse_date(new_due, "新解锁日期")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("调整解锁日期必须说明原因")
        with self.conn:
            self.conn.execute(
                "insert into maintenance_revisions(id,window_id,old_due,new_due,"
                "reason,changed_by,changed_at) values(?,?,?,?,?,?,?)",
                (next_id(self.conn, "MRE"), window_id,
                 win["unlock_due"], due.isoformat(),
                 reason.strip(), actor["id"], now_iso()),
            )
            self.conn.execute(
                "update maintenance_windows set unlock_due=?, status='locked' "
                "where id=?",
                (due.isoformat(), window_id),
            )
        self.supply_changed(actor, f"检修窗口 {window_id} 解锁日期调整：{reason.strip()}")
        return self.get_window(window_id)

    def unlock(self, actor: dict, window_id: str, note: str = "") -> dict:
        """人工确认检修完成、解锁复产（待解锁队列由重启恢复）。"""
        win = self._window_row(window_id)
        if win["status"] not in ("locked", "await_unlock"):
            raise ConflictError("该检修窗口不在停井状态")
        with self.conn:
            self.conn.execute(
                "update maintenance_windows set status='unlocked', unlocked_by=?, "
                "unlocked_at=?, note=coalesce(note,?) where id=?",
                (actor["id"], now_iso(), note or None, window_id),
            )
        self.supply_changed(actor, f"检修窗口 {window_id} 已解锁")
        return self.get_window(window_id)

    def get_window(self, window_id: str) -> dict:
        return dict(self._window_row(window_id))

    def list_windows(self, status: str | None = None) -> list[dict]:
        sql = "select * from maintenance_windows"
        params: tuple = ()
        if status:
            sql += " where status=?"
            params = (status,)
        sql += " order by unlock_due, id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def refresh_window_states(self, today: date | None = None) -> list[str]:
        """把过了解锁日仍锁定的窗口推进到 await_unlock（启动恢复时调用）。"""
        today = today or date.today()
        rows = self.conn.execute(
            "select id from maintenance_windows where status='locked' and unlock_due<=?",
            (today.isoformat(),),
        ).fetchall()
        if rows:
            with self.conn:
                for r in rows:
                    self.conn.execute(
                        "update maintenance_windows set status='await_unlock' where id=?",
                        (r["id"],),
                    )
        return [r["id"] for r in rows]

    # == 管网限输 =============================================================
    def create_curtailment(
        self,
        actor: dict,
        channel_id: str,
        limit_rate: float,
        valid_from: str,
        valid_to: str,
        note: str = "",
    ) -> dict:
        if self.conn.execute(
            "select 1 from export_channels where id=?", (channel_id,)
        ).fetchone() is None:
            raise NotFoundError("外输通道不存在")
        start = parse_date(valid_from, "限输开始日期")
        end = parse_date(valid_to, "限输结束日期")
        if start > end:
            raise ValidationError("限输开始不能晚于结束")
        cid = next_id(self.conn, "CUR")
        with self.conn:
            self.conn.execute(
                "insert into curtailments(id,channel_id,limit_rate,valid_from,valid_to,"
                "status,created_by,created_at,note) values(?,?,?,?,?, 'scheduled',?,?,?)",
                (cid, channel_id, positive_rate(limit_rate, "限输量"),
                 start.isoformat(), end.isoformat(), actor["id"], now_iso(),
                 note or None),
            )
        self.supply_changed(actor, f"通道限输 {cid} 已排定")
        return self.get_curtailment(cid)

    def reschedule_curtailment(
        self,
        actor: dict,
        curtailment_id: str,
        new_from: str,
        new_to: str,
        reason: str,
    ) -> dict:
        cur = self._curtailment_row(curtailment_id)
        if cur["status"] != "scheduled":
            raise ConflictError("已经生效的限输不能改期，只能提前解除")
        if date.fromisoformat(cur["valid_from"]) <= date.today():
            raise ConflictError("限输已进入履行期，不能改期")
        start = parse_date(new_from, "新限输开始日期")
        end = parse_date(new_to, "新限输结束日期")
        if start > end:
            raise ValidationError("限输开始不能晚于结束")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("改期必须说明原因")
        with self.conn:
            self.conn.execute(
                "insert into curtailment_revisions(id,curtailment_id,old_valid_from,"
                "old_valid_to,new_valid_from,new_valid_to,reason,changed_by,changed_at) "
                "values(?,?,?,?,?,?,?,?,?)",
                (next_id(self.conn, "CRE"), curtailment_id,
                 cur["valid_from"], cur["valid_to"],
                 start.isoformat(), end.isoformat(),
                 reason.strip(), actor["id"], now_iso()),
            )
            self.conn.execute(
                "update curtailments set valid_from=?, valid_to=? where id=?",
                (start.isoformat(), end.isoformat(), curtailment_id),
            )
        self.supply_changed(actor, f"限输 {curtailment_id} 改期：{reason.strip()}")
        return self.get_curtailment(curtailment_id)

    def lift_curtailment(self, actor: dict, curtailment_id: str) -> dict:
        cur = self._curtailment_row(curtailment_id)
        if cur["status"] not in ("scheduled", "active"):
            raise ConflictError("限输已解除或已取消")
        with self.conn:
            self.conn.execute(
                "update curtailments set status='lifted' where id=?",
                (curtailment_id,),
            )
        self.supply_changed(actor, f"限输 {curtailment_id} 已解除")
        return self.get_curtailment(curtailment_id)

    def get_curtailment(self, curtailment_id: str) -> dict:
        return dict(self._curtailment_row(curtailment_id))

    def list_curtailments(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "select * from curtailments order by valid_from, id"
            )
        ]

    def refresh_curtailment_states(self, today: date | None = None) -> None:
        today = today or date.today()
        with self.conn:
            self.conn.execute(
                "update curtailments set status='active' where status='scheduled' "
                "and valid_from<=? and valid_to>=?",
                (today.isoformat(), today.isoformat()),
            )
            self.conn.execute(
                "update curtailments set status='cancelled' where status='scheduled' "
                "and valid_to<?",
                (today.isoformat(),),
            )

    # == 当日阻断判定（供 projection 使用）====================================
    def well_blockers(self, well_id: str, day: date) -> list[dict]:
        """当日是否停井。

        - 排定作业只在其计划区间内阻断；进行中作业即使超过计划结束日，
          在登记完工前持续阻断；
        - 检修/待解锁窗口自停井日起持续阻断，直到人工解锁
          （超过应解锁日意味着检修延期，井仍停着）。
        """
        blockers: list[dict] = []
        iso = day.isoformat()
        prog = self.conn.execute(
            "select * from work_programs where well_id=? and status in "
            "('scheduled','in_progress') and planned_start<=? "
            "and (planned_end>=? or status='in_progress')",
            (well_id, iso, iso),
        ).fetchall()
        for r in prog:
            blockers.append({"type": "work_program", "id": r["id"], "kind": r["kind"]})
        win = self.conn.execute(
            "select * from maintenance_windows where well_id=? and status in "
            "('locked','await_unlock') and lock_from<=?",
            (well_id, iso),
        ).fetchall()
        for r in win:
            blockers.append({"type": "maintenance", "id": r["id"]})
        return blockers

    # == 内部 =================================================================
    def _assert_well(self, well_id: str) -> None:
        if self.conn.execute(
            "select 1 from wells where id=?", (well_id,)
        ).fetchone() is None:
            raise NotFoundError("井不存在")

    def _program_row(self, program_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from work_programs where id=?", (program_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("作业方案不存在")
        return row

    def _window_row(self, window_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from maintenance_windows where id=?", (window_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("检修窗口不存在")
        return row

    def _curtailment_row(self, curtailment_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from curtailments where id=?", (curtailment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("限输记录不存在")
        return row
