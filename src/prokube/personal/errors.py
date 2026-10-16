"""Stable user-facing errors for the personal Sandbox API."""


class SandboxError(Exception):
    """An operation could not be completed."""


class AuthenticationRequired(SandboxError):
    """Run pk login, or supply an explicit credential."""


class PermissionDenied(SandboxError):
    """The identity is not allowed to perform this operation."""


class SandboxNotFound(SandboxError):
    """No managed Sandbox exists with this name for this identity."""


class SandboxAlreadyExists(SandboxError):
    """Creation conflicts with an existing personal Sandbox."""


class ConnectionFailed(SandboxError):
    """The platform could not be reached."""


class HostKeyChanged(SandboxError):
    """Previously pinned SSH host identity changed."""
