"""Pydantic models for Sandbox v2 (Firecracker) operations.

The v2 backend contract lives in the pkui ``modules/sandboxv2`` module. The
execute/file request+response shapes are reused verbatim from v1 (imported from
``prokube.sandbox.models``) because the guest ``execd`` speaks the identical HTTP
contract; only the sandbox lifecycle models (create request, sandbox info,
phase) are v2-specific and defined here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# Result/file models are reused verbatim from v1 so the public result surface
# (``.success`` / ``.stdout`` / ``.stderr`` / ``FileInfo``) is identical.
from prokube.sandbox.models import (  # noqa: F401
    CodeResult,
    CommandResult,
    FileInfo,
)


class SandboxV2Status(str, Enum):
    """User-facing lifecycle phase of a Firecracker sandbox.

    Mirrors the backend ``SandboxV2Phase`` vocabulary, which folds the raw
    FirecrackerSandbox status phases (WaitingForImage/Restoring/Hibernating/
    Hibernated/...) onto the same words v1 uses.
    """

    PENDING = "Pending"
    RUNNING = "Running"
    PAUSED = "Paused"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class SandboxV2Info(BaseModel):
    """Information about a Firecracker sandbox (projected from the CR)."""

    name: str = Field(..., description="Sandbox (CR) name")
    namespace: str = Field(..., description="Kubernetes namespace")
    status: SandboxV2Status = Field(
        default=SandboxV2Status.UNKNOWN, description="User-facing phase"
    )
    image: str | None = Field(default=None, description="Base OCI image")
    runtime_class: str | None = Field(
        default=None, description="spec.runtimeClassName (fc-host | fc-pod)"
    )
    operating_mode: str | None = Field(
        default=None, description="spec.operatingMode (Running | Hibernated)"
    )
    node: str | None = Field(default=None, description="Node hosting the microVM")
    endpoint: str | None = Field(
        default=None, description="In-cluster execd Service address"
    )
    terminal_enabled: bool = Field(
        default=True, description="Whether a ttyd Terminal was injected"
    )
    message: str | None = Field(
        default=None, description="Human-readable status detail / failure reason"
    )
    created_at: str | None = Field(default=None, description="Creation timestamp")


class SandboxV2Resources(BaseModel):
    """Guest compute request for a Firecracker sandbox."""

    vcpus: int | None = Field(default=None, ge=1, description="Guest vCPUs")
    mem_mib: int | None = Field(
        default=None, ge=128, description="Guest memory in MiB"
    )


class CreateSandboxV2Request(BaseModel):
    """Request body for ``POST .../sandboxv2`` (mirrors backend model)."""

    name: str = Field(..., min_length=1, max_length=63, description="Sandbox name")
    image: str | None = Field(
        default=None, description="Base OCI image (defaults to pk-sandbox-base)"
    )
    vcpus: int | None = Field(default=None, ge=1, description="Guest vCPUs")
    mem_mib: int | None = Field(
        default=None, ge=128, serialization_alias="memMiB", description="Guest MiB"
    )
    terminal: bool = Field(
        default=True, description="Inject a ttyd Terminal (:7681) into the guest"
    )
    workspace_size: str | None = Field(
        default=None,
        serialization_alias="workspaceSize",
        description="Ephemeral /workspace volume size (e.g. 10Gi)",
    )
    target_node: str | None = Field(
        default=None,
        serialization_alias="targetNode",
        description="Pin the microVM to a node (spec.targetNode)",
    )
    image_pull_secrets: list[str] | None = Field(
        default=None,
        serialization_alias="imagePullSecrets",
        description="Registry pull secret names",
    )
    runtime_class_name: str | None = Field(
        default=None,
        serialization_alias="runtimeClassName",
        description="spec.runtimeClassName (fc-host | fc-pod)",
    )
    operating_mode: str | None = Field(
        default=None,
        serialization_alias="operatingMode",
        description="spec.operatingMode (Running | Hibernated)",
    )
    egress: bool | None = Field(
        default=None,
        description="spec.egress — true lets the microVM reach the cluster/internet",
    )
    volumes: list[dict[str, Any]] | None = Field(
        default=None, description="spec.volumes pass-through (CR-shaped dicts)"
    )
    volume_mounts: list[dict[str, Any]] | None = Field(
        default=None,
        serialization_alias="volumeMounts",
        description="spec.volumeMounts pass-through (CR-shaped dicts)",
    )
    manifest: dict[str, Any] | None = Field(
        default=None,
        description="Full FirecrackerSandbox object; wins over structured knobs",
    )


class ExecV2Request(BaseModel):
    """Request body for ``POST .../sandboxv2/{name}/exec``.

    Matches the backend ``ExecuteCodeRequest`` field names verbatim (snake_case
    on the wire — the v2 exec endpoint does not use camelCase aliases).
    """

    code: str = Field(..., description="Code or command to execute")
    language: str = Field(
        default="bash",
        description="'bash' for raw commands, 'python'/'node' for code",
    )
    timeout: int = Field(default=60, ge=1, le=300, description="Timeout in seconds")
    workdir: str = Field(default="/workspace", description="Working directory")
    use_jupyter: bool = Field(
        default=False, description="Use stateful Jupyter kernel execution"
    )
    session_id: str | None = Field(
        default=None, description="Session ID for stateful Jupyter execution"
    )
    reset_session: bool = Field(
        default=False, description="Restart the Jupyter kernel before executing"
    )


class UploadFileV2Request(BaseModel):
    """Request body for ``POST .../sandboxv2/{name}/files``."""

    path: str = Field(..., description="Destination path in the guest")
    content: str = Field(..., description="File content encoded per ``encoding``")
    encoding: str = Field(
        default="base64", description="Content encoding ('text' or 'base64')"
    )
