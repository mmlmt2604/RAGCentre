"""Exception hierarchy and API error codes (IMPLEMENTATION_PLAN.md section 16).

``RagError`` and its subclasses are turned into ``{"error": {"code", "message", "details"}}``
responses by the API error handler. ``ExecutionError`` is raised inside node-run executors and
provider clients; the queue uses ``retryable`` and ``retry_after`` to decide what to do next.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    INVALID_PIPELINE = "invalid_pipeline"
    INVALID_FILTER = "invalid_filter"
    IMMUTABLE_FIELD = "immutable_field"
    PROVIDER_IN_USE = "provider_in_use"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_VERSION_DRIFT = "provider_version_drift"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INDEX_NOT_BUILT = "index_not_built"
    INDEX_INCOMPLETE = "index_incomplete"
    INDEX_MISMATCH = "index_mismatch"
    EMBEDDING_DIMENSION_MISMATCH = "embedding_dimension_mismatch"
    PIPELINE_ARCHIVED = "pipeline_archived"
    PIPELINE_IS_ACTIVE = "pipeline_is_active"
    SECRET_STORAGE_DISABLED = "secret_storage_disabled"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    READ_ONLY_TOKEN = "read_only_token"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"


class RagError(Exception):
    """An error with an API error code and HTTP status."""

    code: ClassVar[ErrorCode] = ErrorCode.CONFLICT
    status: ClassVar[int] = 400

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.error_code: ErrorCode = code or type(self).code
        self.http_status: int = status or type(self).status

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.error_code.value, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return {"error": body}


class NotFound(RagError):
    code = ErrorCode.NOT_FOUND
    status = 404


class ValidationFailed(RagError):
    code = ErrorCode.VALIDATION_ERROR
    status = 422


class InvalidPipeline(RagError):
    code = ErrorCode.INVALID_PIPELINE
    status = 422


class InvalidFilter(RagError):
    code = ErrorCode.INVALID_FILTER
    status = 422


class ImmutableField(RagError):
    code = ErrorCode.IMMUTABLE_FIELD
    status = 409


class ProviderInUse(RagError):
    code = ErrorCode.PROVIDER_IN_USE
    status = 409


class ProviderFailed(RagError):
    """A provider call failed on the query path (502)."""

    code = ErrorCode.PROVIDER_ERROR
    status = 502


class Conflict(RagError):
    code = ErrorCode.CONFLICT
    status = 409


class Unauthorized(RagError):
    code = ErrorCode.UNAUTHORIZED
    status = 401


class Forbidden(RagError):
    code = ErrorCode.FORBIDDEN
    status = 403


class ReadOnlyToken(RagError):
    """The token may read but this action writes. 403, distinct code from a plain refusal."""

    code = ErrorCode.READ_ONLY_TOKEN
    status = 403


class PayloadTooLarge(RagError):
    code = ErrorCode.PAYLOAD_TOO_LARGE
    status = 413


class UnsupportedMediaType(RagError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE
    status = 415


class SecretStorageDisabled(RagError):
    code = ErrorCode.SECRET_STORAGE_DISABLED
    status = 409


class GuardError(RagError):
    """A mismatch-guard refusal (section 6). The code is given per instance."""

    code = ErrorCode.INDEX_MISMATCH
    status = 409


class CollectionMismatch(Exception):
    """A vector store collection exists with a different dimension or metric."""


class ExecutionError(Exception):
    """An error during node-run execution or a provider call.

    ``retryable`` follows section 8: connection errors, timeouts, 408, 429 and 5xx are
    retryable; other 4xx and invalid responses are permanent.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        provider: str | None = None,
        upstream_status: int | None = None,
        upstream_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        # What the provider service answered, when the error came from one.
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.provider = provider


def permanent(code: ErrorCode | str, message: str, provider: str | None = None) -> ExecutionError:
    return ExecutionError(str(code), message, retryable=False, provider=provider)
