"""身份、角色、职责与合同可见范围。

角色：
- rig         井队
- geology     地质与储层（可提交解释/测试/产能例外，不能审批自己提交的）
- maintenance 维护（登记检修、解锁）
- pipeline    输气运行（通道、限输）
- dispatch    生产调度（复核测试、审批例外、供气决定）
- commercial  商业用户（只能看到与自己合同有关的供气信息）
"""

from __future__ import annotations

import hmac
import sqlite3

from .errors import AuthError, PermissionError, ValidationError
from .util import next_id, now_iso, require_fields, token_hash

ROLES = frozenset(
    {"rig", "geology", "maintenance", "pipeline", "dispatch", "commercial"}
)
# 可以使用系统的业务角色（商业用户可见范围受限）
INTERNAL_ROLES = frozenset(
    {"rig", "geology", "maintenance", "pipeline", "dispatch"}
)

# 各服务在分发层（api.py）做动作-角色映射；这里提供查询辅助
ALL_ACTIONS_PERMISSIONS: dict[str, frozenset[str]] = {
    "catalog:manage": frozenset({"dispatch"}),
    "materials:receive": frozenset({"rig", "geology", "maintenance"}),
    "materials:resolve": frozenset({"dispatch"}),
    "interpretation:submit": frozenset({"geology"}),
    "test:submit": frozenset({"geology", "rig"}),
    "test:review": frozenset({"dispatch"}),
    "offer:publish": frozenset({"dispatch"}),
    "exception:submit": frozenset({"geology"}),
    "exception:review": frozenset({"dispatch"}),
    "program:manage": frozenset({"rig", "maintenance", "dispatch"}),
    "window:manage": frozenset({"maintenance", "dispatch"}),
    "window:unlock": frozenset({"maintenance", "dispatch"}),
    "channel:manage":       frozenset({"pipeline", "dispatch"}),
    "curtailment:manage":   frozenset({"pipeline", "dispatch"}),
    "report:submit": frozenset({"rig", "maintenance", "dispatch"}),
    "report:correct": frozenset({"dispatch"}),
    "commitment:manage":    frozenset({"dispatch"}),
    "decision:make":        frozenset({"dispatch"}),
    "queue:view":           frozenset({"dispatch", "rig", "geology", "maintenance", "pipeline"}),
}


class AuthService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 用户与令牌 ----------------------------------------------------------
    def create_user(
        self, name: str, role: str, *, actor: dict | None = None
    ) -> tuple[str, str]:
        """新建用户，返回 (用户编号, 明文令牌)。明文令牌只在创建时返回一次。"""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("用户名不能为空")
        if role not in ROLES:
            raise ValidationError(f"未知角色：{role}")
        import secrets

        uid = next_id(self.conn, "USR")
        raw_token = secrets.token_hex(24)
        with self.conn:
            self.conn.execute(
                "insert into users(id,name,role,token_hash,active,created_at) "
                "values(?,?,?,?,1,?)",
                (uid, name.strip(), role, token_hash(raw_token), now_iso()),
            )
        return uid, raw_token

    def authenticate(self, raw_token: str) -> dict:
        """常量时间比对令牌，返回用户上下文；商业用户自动加载合同范围。"""
        if not raw_token:
            raise AuthError("缺少认证令牌")
        target = token_hash(raw_token)
        rows = self.conn.execute("select * from users where active=1").fetchall()
        for row in rows:
            if hmac.compare_digest(row["token_hash"], target):
                ctx = dict(row)
                contract_rows = self.conn.execute(
                    "select id from contracts where user_id=?", (row["id"],)
                ).fetchall()
                ctx["contract_ids"] = frozenset(r["id"] for r in contract_rows)
                return ctx
        raise AuthError("令牌无效或已停用")

    def require(self, actor: dict, action: str) -> None:
        role = actor["role"]
        allowed = ALL_ACTIONS_PERMISSIONS.get(action)
        if allowed is None or role not in allowed:
            disp = action.rsplit(":", 1)[-1]
            raise PermissionError(f"当前角色无权执行此操作（{disp}）")

    def assert_not_self(self, actor: dict, submitter_id: str, what: str) -> None:
        """复核/审批人不得是提交人本人。

        地质人员不能批准自己提交的产能例外，调度也不能复核自己提交的测试。
        """
        if actor["id"] == submitter_id:
            raise PermissionError(f"不能审批自己提交的{what}")

    # -- 合同 ----------------------------------------------------------------
    def create_contract(self, name: str, user_id: str, channel_id: str) -> str:
        user = self.conn.execute(
            "select * from users where id=?", (user_id,)
        ).fetchone()
        if user is None:
            raise ValidationError("关联用户不存在")
        if user["role"] != "commercial":
            raise ValidationError("合同只能归属商业用户")
        if self.conn.execute(
            "select 1 from export_channels where id=?", (channel_id,)
        ).fetchone() is None:
            raise ValidationError("合同交付通道不存在")
        cid = next_id(self.conn, "CTR")
        with self.conn:
            self.conn.execute(
                "insert into contracts(id,name,user_id,channel_id,created_at) "
                "values(?,?,?,?,?)",
                (cid, name, user_id, channel_id, now_iso()),
            )
        return cid

    def assert_contract_visible(self, actor: dict, contract_id: str) -> None:
        if actor["role"] == "commercial" and contract_id not in actor.get(
            "contract_ids", frozenset()
        ):
            raise PermissionError("只能查看与本合同有关的供气信息")

    def assert_internal(self, actor: dict) -> None:
        if actor["role"] == "commercial":
            raise PermissionError("商业用户不能访问内部生产数据")

    def list_contracts(self, actor: dict) -> list[dict]:
        if actor["role"] == "commercial":
            rows = self.conn.execute(
                "select * from contracts where user_id=?", (actor["id"],)
            ).fetchall()
        else:
            rows = self.conn.execute("select * from contracts").fetchall()
        return [dict(r) for r in rows]
