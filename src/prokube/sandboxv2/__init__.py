"""Sandbox v2 (Firecracker) module for the prokube SDK.

A parallel client to :mod:`prokube.sandbox`, targeting the Firecracker-backed
``sandboxv2`` endpoints. Reuses ``prokube.common`` (auth, http, config,
exceptions) verbatim and mirrors the v1 public surface, adapted for microVMs
(fc-pod is the only runtime, same ``workspace`` param as v1). There is no warm
pool; instead a running sandbox can be snapshotted into a reusable
FirecrackerImage (:meth:`SandboxV2.snapshot`) and a later sandbox can
resume-clone from it (:meth:`SandboxV2.from_snapshot`).
"""

from prokube.sandboxv2.client import SandboxV2Client
from prokube.sandboxv2.models import (
    DNSConfig,
    DNSConfigOption,
    ExecAction,
    HTTPGetAction,
    HTTPHeader,
    Lifecycle,
    LifecycleHandler,
    Probe,
    SnapshotImage,
    TCPSocketAction,
)
from prokube.sandboxv2.sandbox import SandboxV2

__all__ = [
    "SandboxV2",
    "SandboxV2Client",
    "Probe",
    "Lifecycle",
    "LifecycleHandler",
    "HTTPGetAction",
    "HTTPHeader",
    "TCPSocketAction",
    "ExecAction",
    "DNSConfig",
    "DNSConfigOption",
    "SnapshotImage",
]
