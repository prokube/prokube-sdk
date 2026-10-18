"""Egress rules for set_network(), as the JSON of Substrate ``EgressRule``s."""

from __future__ import annotations

from typing import Any


def passthrough(*hostnames: str, port: int = 443) -> dict[str, Any]:
    """Let TLS connections to the hostnames through unchanged."""
    return {
        "tlsPassthrough": {"hostnames": list(hostnames), "ports": {"numbers": [port]}}
    }


def inject(
    hostname: str,
    header: str,
    credential_uri: str,
    *,
    prefix: str = "",
    port: int = 443,
) -> dict[str, Any]:
    """HTTPS to hostname, with header set to a credential at egress.

    The sandbox sends the header with any placeholder; the egress gateway
    replaces its value with prefix + the credential (see secret_uri()).
    """
    replace = {"header": header, "credentialUri": credential_uri}
    if prefix:
        replace["prefix"] = prefix
    return {
        "https": {
            "hostnames": [hostname],
            "ports": {"numbers": [port]},
            "effects": {"replaceHeaders": [replace]},
        }
    }
