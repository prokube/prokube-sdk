"""Authentication helpers for prokube SDK."""

from __future__ import annotations

from prokube.common.config import Config
from prokube.common.exceptions import AuthenticationError


def get_auth_headers(config: Config) -> dict[str, str]:
    """Get authentication headers for API requests.

    Args:
        config: SDK configuration containing user_id.

    Returns:
        Dictionary of headers to include in API requests.

    Raises:
        AuthenticationError: If no user_id is available.
    """
    if config.user_id:
        return {"kubeflow-userid": config.user_id}

    raise AuthenticationError(
        "No user ID available for authentication. "
        "Set PROKUBE_USER_ID environment variable, "
        "or pass user_id parameter to the Sandbox."
    )
