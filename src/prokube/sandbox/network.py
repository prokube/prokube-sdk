"""Egress rules for set_network(), as the JSON of Substrate ``EgressRule``s."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
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


def add_rules(
    rules: Iterable[Mapping[str, Any]], *new: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return rules with the new ones appended; they take their hosts along.

    Substrate allows each host name and port in one rule only, so the new
    rules' host names are removed from the other rules on the same ports, and
    rules left without host names are dropped. The inputs are not changed.
    """
    claimed = [
        (set(spec.get("hostnames", [])), _ports(spec))
        for rule in new
        for spec in rule.values()
    ]
    kept = []
    for rule in rules:
        rule = copy.deepcopy(dict(rule))
        for spec in rule.values():
            if "hostnames" in spec:
                spec["hostnames"] = [
                    host
                    for host in spec["hostnames"]
                    if not any(
                        host in hosts and _overlap(ports, _ports(spec))
                        for hosts, ports in claimed
                    )
                ]
        if all(spec.get("hostnames", True) for spec in rule.values()):
            kept.append(rule)
    return kept + [copy.deepcopy(dict(rule)) for rule in new]


def _ports(spec: Mapping[str, Any]) -> set[int]:
    """The rule's port numbers; empty when it names none (any port)."""
    return set(spec.get("ports", {}).get("numbers", []))


def _overlap(a: set[int], b: set[int]) -> bool:
    return not a or not b or bool(a & b)
