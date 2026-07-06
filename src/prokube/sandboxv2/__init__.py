"""Sandbox v2 (Firecracker) module for the prokube SDK.

A parallel client to :mod:`prokube.sandbox`, targeting the Firecracker-backed
``sandboxv2`` endpoints. Reuses ``prokube.common`` (auth, http, config,
exceptions) verbatim and mirrors the v1 public surface, adapted for microVMs
(``runtime_class`` fc-host/fc-pod, same ``workspace`` param as v1, warm pool via
FirecrackerPool with a ``warm_state`` Hibernated/Running knob).
"""

from prokube.sandboxv2.client import SandboxV2Client
from prokube.sandboxv2.models import (
    ExecAction,
    HTTPGetAction,
    HTTPHeader,
    Lifecycle,
    LifecycleHandler,
    Probe,
    TCPSocketAction,
)
from prokube.sandboxv2.pool import SandboxV2Pool
from prokube.sandboxv2.sandbox import SandboxV2

__all__ = [
    "SandboxV2",
    "SandboxV2Client",
    "SandboxV2Pool",
    "Probe",
    "Lifecycle",
    "LifecycleHandler",
    "HTTPGetAction",
    "HTTPHeader",
    "TCPSocketAction",
    "ExecAction",
]
