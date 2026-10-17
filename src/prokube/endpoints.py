"""Every platform address the SDK uses, derived from the platform URL.

The platform URL (``https://<domain>``) is the only input. The Agent Gateway
routes are still being settled in prokube/prokube-neo#87 and #97; when they
change, this module is the only place to adjust.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Public Dex client for native login, created by prokube/prokube-neo#97.
# The SDK sends the Dex ID token, whose audience is this client id, so Agent
# Gateway and ate-api must accept that audience.
DEX_CLIENT_ID = "prokube-cli"

# Must match the client's registered redirectURIs exactly. Tried in order;
# the first free port wins.
REDIRECT_URIS = (
    "http://localhost:8000/callback",
    "http://localhost:18000/callback",
)

# offline_access makes Dex return a refresh token.
SCOPES = "openid profile email offline_access"

# Atespace holding the approved sandbox templates, viewable by every
# workspace (prokube/prokube-neo#98).
TEMPLATE_ATESPACE = "sandbox-templates"


def normalize_url(url: str) -> str:
    """Check that url is an HTTPS platform URL and strip the trailing slash."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Expected an https://<domain> platform URL, got {url!r}")
    return url.rstrip("/")


def dex_issuer(url: str) -> str:
    """OIDC issuer of the platform's Dex."""
    return f"{normalize_url(url)}/dex"


def ate_api_target(url: str) -> str:
    """gRPC target of ate-api (``ateapi.Control``) behind Agent Gateway."""
    return _grpc_target(url)


def env_target(url: str) -> str:
    """gRPC target of the env guest API (process and file services).

    Calls carry ``x-env-id`` and ``x-env-atespace`` metadata, which the
    gateway uses to reach the Actor.
    """
    return _grpc_target(url)


def _grpc_target(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return f"{parsed.hostname}:{parsed.port or 443}"
