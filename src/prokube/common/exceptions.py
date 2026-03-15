"""Exception classes for prokube SDK."""


class ProKubeError(Exception):
    """Base exception for all prokube SDK errors."""

    pass


class AuthenticationError(ProKubeError):
    """Raised when authentication fails or credentials are missing."""

    pass


class SandboxError(ProKubeError):
    """Base exception for sandbox-related errors."""

    pass


class SandboxNotFoundError(SandboxError):
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
    """Raised when no sandboxes are available in the pool."""

    pass
