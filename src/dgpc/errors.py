"""领域错误与错误码。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则违背的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        result = {"error": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class AuthError(DomainError):
    code = "auth_error"
    http_status = 401


class PermissionError(DomainError):  # noqa: A001 - 领域内固定命名
    code = "permission_denied"
    http_status = 403


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class QuarantineError(DomainError):
    code = "material_quarantined"
    http_status = 409
