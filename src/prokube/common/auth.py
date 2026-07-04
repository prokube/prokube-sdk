"""Authentication helpers for prokube SDK."""

from __future__ import annotations

from prokube.common.config import Config


def get_auth_headers(config: Config) -> dict[str, str]:
    """Get authentication headers for API requests.

    If api_key is set, uses API key authentication (x-api-key header).
    Otherwise falls back to user_id authentication (kubeflow-userid header) when
    provided. In-cluster Agent Gateway access can run without SDK auth headers.

    Args:
        config: SDK configuration containing api_key and/or user_id.

    Returns:
        Dictionary of headers to include in API requests.

    """
    if config.api_key:
        return {"x-api-key": config.api_key}
    if config.user_id:
        return {"kubeflow-userid": config.user_id}

    return {}
