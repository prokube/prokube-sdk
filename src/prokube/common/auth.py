"""Authentication helpers for prokube SDK."""

from __future__ import annotations

from prokube.common.config import Config
from prokube.common.exceptions import AuthenticationError


def get_auth_headers(config: Config) -> dict[str, str]:
    """Get authentication headers for API requests.

    If api_key is set, uses API key authentication (x-api-key header).
    Otherwise falls back to user_id authentication (kubeflow-userid header).

    Args:
        config: SDK configuration containing api_key and/or user_id.

    Returns:
        Dictionary of headers to include in API requests.

    Raises:
        AuthenticationError: If neither api_key nor user_id is available.
    """
    if config.api_key:
        return {"x-api-key": config.api_key}
    if config.user_id:
        return {"kubeflow-userid": config.user_id}

    raise AuthenticationError(
        "No api_key or user_id available for authentication. "
        "Set PROKUBE_API_KEY, PROKUBE_USER_ID, or KF_USER environment variable, "
        "or pass api_key/user_id parameter to the Sandbox."
    )
