"""Every platform address the SDK uses, derived from the platform URL.

The platform URL (``https://<domain>``) is the only input. Without one, or
with an explicit ``endpoint``, the SDK calls Agent Gateway's in-cluster
Service instead (``IN_CLUSTER_GATEWAY`` by default). The Agent Gateway
routes are defined in prokube/prokube-neo#97 (``paas/substrate/README.md``,
"Endpoints for CLI and SDK"); when they change, this module is the only place
to adjust.
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

# Atespace holding the presets' templates, viewable by every workspace
# (prokube/prokube-neo#98, #132).
TEMPLATE_ATESPACE = "sandbox-templates"


# Agent Gateway's in-cluster Service, used without a platform URL: from
# workspace pods (workload identity, prokube/prokube-neo#100) and from servers
# such as pkui. Same routes as the public endpoint; plaintext HTTP/2, which
# the Istio sidecars encrypt.
IN_CLUSTER_GATEWAY = "agentgateway-proxy.agentgateway-system.svc.cluster.local:80"

# Port of the runtime image's WebSocket bridge to sshd (websocat on :8022, path
# /ssh, prokube/sandbox-images#16).
SSH_PORT = 8022


def normalize_url(url: str) -> str:
    """Check that url is an HTTPS platform URL and strip the trailing slash."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Expected an https://<domain> platform URL, got {url!r}")
    return url.rstrip("/")


def dex_issuer(url: str) -> str:
    """OIDC issuer of the platform's Dex."""
    return f"{normalize_url(url)}/dex"


def grpc_target(url: str) -> str:
    """gRPC target of ate-api (``ateapi.Control``) and the env guest API.

    Both are behind Agent Gateway. Env calls carry ``x-env-id`` and
    ``x-env-atespace`` metadata, which the gateway uses to reach the Actor.
    """
    parsed = urlparse(normalize_url(url))
    return f"{parsed.hostname}:{parsed.port or 443}"


def presets_url(base: str) -> str:
    """The platform's sandbox presets (prokube/prokube-neo#132).

    ``base`` is the platform URL or ``http://<in-cluster endpoint>``.
    """
    return f"{base}/svc/sandbox/v1alpha/presets"


def ssh_url(url: str, atespace: str, actor: str) -> str:
    """WebSocket URL of an Actor's SSH bridge, used by ``pk sandbox proxy``.

    Agent Gateway forwards ``/svc/sandbox/`` to ate-env-api's port proxy
    (path form) after the data-plane check.
    """
    host = normalize_url(url).removeprefix("https://")
    return (
        f"wss://{host}/svc/sandbox/v1alpha/atespaces/{atespace}/envs/{actor}"
        f"/ports/{SSH_PORT}/ssh"
    )
