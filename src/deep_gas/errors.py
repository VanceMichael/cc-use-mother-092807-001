"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务违规的基类，错误信息可直接返回值班人员。"""


class NotFound(DomainError):
    """引用的资源不存在。"""


class Conflict(DomainError):
    """违反唯一性、版本顺序或状态机约束。"""


class AuthorizationError(DomainError):
    """身份、角色或数据范围不允许该操作。"""


class Quarantined(DomainError):
    """材料编号冲突（同号异值），已进入隔离区。"""


class ValidationError(DomainError):
    """入参不满足领域约束。"""
