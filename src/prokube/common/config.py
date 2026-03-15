"""Configuration handling for prokube SDK."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """Configuration for prokube SDK.

    Configuration can be provided explicitly or via environment variables:
    - PROKUBE_API_URL: Base URL for the prokube API
    - PROKUBE_NAMESPACE: Kubernetes namespace
    - PROKUBE_USER_ID: User ID for authentication
    - PROKUBE_TIMEOUT: Default timeout in seconds (default: 300)
    """

    api_url: str = field(default_factory=lambda: _get_api_url())
    namespace: str = field(default_factory=lambda: _get_namespace())
    user_id: str | None = field(default_factory=lambda: _get_user_id())
    timeout: int = field(default_factory=lambda: _get_timeout())

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.api_url:
            raise ValueError(
                "API URL is required. Set PROKUBE_API_URL environment variable "
                "or pass api_url parameter."
            )
        if not self.namespace:
            raise ValueError(
                "Namespace is required. Set PROKUBE_NAMESPACE environment variable "
                "or pass namespace parameter."
            )
        # Ensure api_url doesn't have trailing slash
        self.api_url = self.api_url.rstrip("/")


def _get_api_url() -> str:
    """Get API URL from environment."""
    return os.environ.get("PROKUBE_API_URL", "")


def _get_namespace() -> str:
    """Get namespace from environment."""
    return os.environ.get("PROKUBE_NAMESPACE", "")


def _get_user_id() -> str | None:
    """Get user ID from environment.

    Tries multiple sources:
    1. PROKUBE_USER_ID (explicit)
    2. NB_USER (JupyterLab/DevLab)
    """
    if user_id := os.environ.get("PROKUBE_USER_ID"):
        return user_id
    if user_id := os.environ.get("NB_USER"):
        return user_id
    return None


def _get_timeout() -> int:
    """Get default timeout from environment."""
    timeout_str = os.environ.get("PROKUBE_TIMEOUT", "300")
    try:
        return int(timeout_str)
    except ValueError:
        return 300
