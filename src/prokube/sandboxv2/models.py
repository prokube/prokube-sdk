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

from pydantic import BaseModel, Field, field_validator

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


class EnvVar(BaseModel):
    """A literal environment variable baked into the guest (spec.env entry).

    Mirrors the Kubernetes ``EnvVar`` (name/value) shape one-for-one so the
    serialized JSON matches the FirecrackerSandbox CRD ``spec.env`` directly.
    """

    name: str = Field(..., description="Environment variable name")
    value: str = Field(..., description="Environment variable value")


class SecretEnvSource(BaseModel):
    """Reference to a Secret by name (spec.envFrom[].secretRef)."""

    name: str = Field(..., description="Secret name in the sandbox namespace")


class EnvFromSource(BaseModel):
    """A ``spec.envFrom`` entry — inject all keys from a Secret as env vars.

    Mirrors the Kubernetes ``EnvFromSource`` shape (``{secretRef: {name}}``) so
    the serialized JSON matches the FirecrackerSandbox CRD ``spec.envFrom``.
    """

    secret_ref: SecretEnvSource = Field(
        ...,
        serialization_alias="secretRef",
        description="Secret whose keys are injected into the guest environment",
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
    env: list[EnvVar] | None = Field(
        default=None,
        description="spec.env — literal env vars ({name,value}) baked into the guest",
    )
    env_from: list[EnvFromSource] | None = Field(
        default=None,
        serialization_alias="envFrom",
        description="spec.envFrom — inject all keys from the named Secret(s)",
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

    @field_validator("env", mode="before")
    @classmethod
    def _coerce_env(cls, v: Any) -> Any:
        """Accept a ``dict[str,str]`` or ``list[{name,value}]`` for env vars.

        A mapping is expanded to CRD-shaped ``[{name, value}]`` entries (values
        stringified); a list of dicts / :class:`EnvVar` is passed through.
        """
        if isinstance(v, dict):
            return [{"name": k, "value": str(val)} for k, val in v.items()]
        return v

    @field_validator("env_from", mode="before")
    @classmethod
    def _coerce_env_from(cls, v: Any) -> Any:
        """Accept a ``list[str]`` of Secret names for ``envFrom``.

        Each bare Secret name is wrapped into a CRD-shaped
        ``{secretRef: {name}}`` entry. Already-shaped dicts / model instances
        pass through unchanged.
        """
        if v is None or not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append(EnvFromSource(secret_ref=SecretEnvSource(name=item)))
            elif isinstance(item, dict) and "secretRef" in item:
                # CR-shaped {"secretRef": {"name": ...}} — normalize the key.
                out.append(
                    EnvFromSource(secret_ref=SecretEnvSource(**item["secretRef"]))
                )
            else:
                out.append(item)
        return out


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


# =============================================================================
# FirecrackerHibernatedPool — warm pool of pre-hibernated sandboxes
#
# Mirrors the pkui backend ``modules/sandboxv2`` pool DTOs
# (CreateHibernatedPoolRequest / HibernatedPoolInfo / HibernatedPoolMember).
# The backend routes live at ``/api/namespaces/{ns}/sandboxv2-pools`` (note:
# ``sandboxv2-pools``, a sibling collection of ``sandboxv2``, NOT a sub-path).
# =============================================================================


class CreateHibernatedPoolRequest(BaseModel):
    """Request body for ``POST .../sandboxv2-pools`` (mirrors backend model).

    ``template`` is a full v2 sandbox create spec reused verbatim — the same
    knobs as :class:`CreateSandboxV2Request` (image, resources, egress,
    volumes/volumeMounts, targetNode). The backend forces the template to
    ``runtimeClassName: fc-host`` and owns ``operatingMode`` (the pool
    controller drives each member to Hibernated), and ignores the template's own
    ``name`` (members are named by the controller).
    """

    name: str = Field(..., min_length=1, max_length=63, description="Pool name")
    size: int = Field(..., ge=0, description="Desired number of warm members")
    template: CreateSandboxV2Request = Field(
        ..., description="Sandbox spec used as the pool member template"
    )


class HibernatedPoolMember(BaseModel):
    """One entry of FirecrackerHibernatedPool ``status.members``."""

    name: str = Field(..., description="Member FirecrackerSandbox name")
    phase: str | None = Field(
        default=None, description="Member FirecrackerSandbox status.phase"
    )


class HibernatedPoolInfo(BaseModel):
    """A FirecrackerHibernatedPool projected from the CR (mirrors backend)."""

    name: str = Field(..., description="Pool (CR) name")
    namespace: str = Field(..., description="Kubernetes namespace")
    size: int = Field(default=0, description="spec.size — desired warm members")
    ready_members: int = Field(
        default=0, description="status.readyMembers — claimable warm members"
    )
    members: list[HibernatedPoolMember] = Field(
        default_factory=list, description="status.members"
    )
    image: str | None = Field(default=None, description="Template base OCI image")
    runtime_class_name: str | None = Field(
        default=None, description="Template spec.runtimeClassName (fc-host)"
    )
    message: str | None = Field(
        default=None, description="Human-readable status detail"
    )
    created_at: str | None = Field(default=None, description="Creation timestamp")
