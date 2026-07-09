"""Exception classes for prokube SDK."""


class ProKubeError(Exception):
    """Base exception for all prokube SDK errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class AuthenticationError(ProKubeError):
    """Raised when authentication fails or credentials are missing."""

    pass


class NotFoundError(ProKubeError):
    """Raised when a resource is not found (HTTP 404)."""

    pass


class SandboxError(ProKubeError):
    """Base exception for sandbox-related errors."""

    pass


class SandboxNotFoundError(SandboxError, NotFoundError):
    """Raised when a sandbox cannot be found."""

    pass


class SandboxTimeoutError(SandboxError):
    """Raised when a sandbox operation times out."""

    pass


class SandboxExecutionError(SandboxError):
    """Raised when code execution in a sandbox fails."""

    pass


class PoolNotFoundError(SandboxError):
    """Raised when a warm pool cannot be found."""

    pass


class PoolExhaustedError(SandboxError):
    """Raised when no warm pool capacity is currently available.

    The condition is retryable. If the backend provides a Retry-After header,
    it is exposed as ``retry_after``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = 429,
        reason: str | None = "pool_exhausted",
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, reason=reason)
        self.retry_after = retry_after
