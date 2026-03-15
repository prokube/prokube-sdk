"""Configuration handling for prokube SDK."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """Configuration for prokube SDK.

    Configuration can be provided explicitly or via environment variables:
    - PROKUBE_API_URL: Base URL for the prokube API
    - PROKUBE_WORKSPACE: Workspace (Kubernetes namespace)
    - PROKUBE_USER_ID: User ID for authentication
    - PROKUBE_TIMEOUT: Default timeout in seconds (default: 300)
    """

    api_url: str = field(default_factory=lambda: _get_api_url())
    workspace: str = field(default_factory=lambda: _get_workspace())
    user_id: str | None = field(default_factory=lambda: _get_user_id())
    timeout: int = field(default_factory=lambda: _get_timeout())

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.api_url:
            raise ValueError(
                "API URL is required. Set PROKUBE_API_URL environment variable "
                "or pass api_url parameter."
            )
        if not self.workspace:
            raise ValueError(
                "Workspace is required. Set PROKUBE_WORKSPACE environment variable "
                "or pass workspace parameter."
            )
        # Ensure api_url doesn't have trailing slash
        self.api_url = self.api_url.rstrip("/")


def _get_api_url() -> str:
    """Get API URL from environment."""
    return os.environ.get("PROKUBE_API_URL", "")


def _get_workspace() -> str:
    """Get workspace from environment."""
    return os.environ.get("PROKUBE_WORKSPACE", "")


def _get_user_id() -> str | None:
    """Get user ID from environment.

    Tries the following sources in order:
    1. PROKUBE_USER_ID (explicit configuration)
    2. KF_USER (Kubeflow user email/name, set by some Kubeflow deployments)

    Returns None if neither is set. In this case, you must provide user_id
    explicitly when creating a Sandbox, or set PROKUBE_USER_ID.

    Note: NB_USER is not used because it's typically "jovyan" in Jupyter
    environments, which doesn't match the actual Kubeflow user.
    """
    if user_id := os.environ.get("PROKUBE_USER_ID"):
        return user_id
    if user_id := os.environ.get("KF_USER"):
        return user_id
    return None


def _get_timeout() -> int:
    """Get default timeout from environment."""
    timeout_str = os.environ.get("PROKUBE_TIMEOUT", "300")
    try:
        return int(timeout_str)
    except ValueError:
        return 300
