"""Python SDK for prokube.ai platform."""

from typing import TYPE_CHECKING

from prokube._version import __version__

if TYPE_CHECKING:
    from prokube.personal import AsyncSandboxClient, SandboxClient

__all__ = ["__version__", "SandboxClient", "AsyncSandboxClient"]


def __getattr__(name):
    if name in ("SandboxClient", "AsyncSandboxClient"):
        from prokube import personal

        return getattr(personal, name)
    raise AttributeError(name)
