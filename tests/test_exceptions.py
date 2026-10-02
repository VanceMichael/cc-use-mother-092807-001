"""产能例外的职责分离与供应口径替代。"""

from src.deep_gas import supply, testing
from src.deep_gas.capacity_exceptions import review_exception, submit_exception
from src.deep_gas.errors import AuthorizationError, Conflict

from domain_fixture import DomainFixture


class ExceptionTest(DomainFixture):
    def setUp(self) -> None:
        super().setUp()
        testing.register_baseline(self.conn, self.actors["disp"], "W1", 50.0, "2026-09-01")
        self.ex = submit_exception(
            self.conn, self.actors["geo"], "W1", 70.0,
            "2026-10-20", "2026-10-22", "措施窗口期产能上调")

    def test_geologist_cannot_approve_own_exception(self) -> None:
        with self.assertRaises(AuthorizationError):  # 地质岗位无审批权
            review_exception(self.conn, self.actors["geo"], self.ex["id"], True)

    def test_submitter_identity_blocked_even_in_review_role(self) -> None:
        # 即便提交人换到可审批岗位，也不能审批自己提交的申请
        # （此处用计划岗审批他人申请应成功，自批通过另一身份场景由自批约束保证）
        other = submit_exception(
            self.conn, self.actors["drill"], "W1", 60.0,
            "2026-11-01", "2026-11-02", "井队申请")
        # 构造同 id 自批：提交人本人无论在哪个审批岗位都被拒
        self.conn.execute(
            "UPDATE exception_requests SET submitted_by='plan' WHERE id=?", (other["id"],))
        with self.assertRaises(Conflict):
            review_exception(self.conn, self.actors["plan"], other["id"], True)

    def test_independent_approval_overrides_baseline_in_window(self) -> None:
        review_exception(self.conn, self.actors["plan"], self.ex["id"], True, "计划复核")
        inside = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-21")
        outside = supply.evaluate_commitment(self.conn, self.actors["disp"], "CM1", "2026-10-23")
        # 窗口内例外替代基线（70 而非 50+70 重复累计）
        self.assertEqual(inside["total_available"], 70.0)
        self.assertEqual(outside["total_available"], 50.0)

    def test_double_review_rejected(self) -> None:
        review_exception(self.conn, self.actors["plan"], self.ex["id"], False)
        with self.assertRaises(Conflict):
            review_exception(self.conn, self.actors["plan"], self.ex["id"], True)
