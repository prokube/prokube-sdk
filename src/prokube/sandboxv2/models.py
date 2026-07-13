"""Pydantic models for Sandbox v2 (Firecracker) operations.

The v2 backend contract lives in the pkui ``modules/sandboxv2`` module. The
execute/file request+response shapes are reused verbatim from v1 (imported from
``prokube.sandbox.models``) because the guest ``execd`` speaks the identical HTTP
contract; only the sandbox lifecycle models (create request, sandbox info,
phase) are v2-specific and defined here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class SandboxV2Condition(BaseModel):
    """A Pod-shaped status condition (FirecrackerSandbox ``status.conditions[]``).

    The stack surfaces two types:

    - ``VMStarted`` — the microVM process is up.
    - ``Ready`` — the guest image passed its (optional) ``startupProbe``; the
      sandbox is fully usable. With no probe, ``Ready`` flips ~immediately after
      ``VMStarted``. Hibernated → both are ``False`` (reason ``Hibernated``).
    """

    type: str = Field(..., description="Condition type (VMStarted | Ready)")
    status: str = Field(..., description='Condition status ("True" | "False")')
    reason: str | None = Field(default=None, description="Machine-readable reason")
    message: str | None = Field(default=None, description="Human-readable detail")
    last_transition_time: str | None = Field(
        default=None,
        alias="lastTransitionTime",
        description="RFC3339 timestamp of the last status change",
    )

    model_config = ConfigDict(populate_by_name=True)

    @property
    def is_true(self) -> bool:
        """Whether ``status`` is exactly ``"True"``."""
        return self.status == "True"


class SandboxV2Info(BaseModel):
    """Information about a Firecracker sandbox (projected from the CR)."""

    name: str = Field(..., description="Sandbox (CR) name")
    workspace: str = Field(..., description="Workspace (Kubernetes namespace)")
    status: SandboxV2Status = Field(
        default=SandboxV2Status.UNKNOWN, description="User-facing phase"
    )
    conditions: list[SandboxV2Condition] = Field(
        default_factory=list,
        description="Pod-shaped status conditions (VMStarted / Ready)",
    )
    ready: bool = Field(
        default=False,
        description=(
            "Whether the sandbox is fully ready (the Ready condition is True). "
            "phase == Running alone is NOT sufficient — it means only that the "
            "VM process started."
        ),
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
    mem_mib: int | None = Field(default=None, ge=128, description="Guest memory in MiB")
    overlay_mib: int | None = Field(
        default=None,
        ge=64,
        le=262144,
        description="Writable-overlay (rootfs scratch) cap in MiB. Sparse/thin-"
        "provisioned, so a ceiling not a reservation. Omitted -> CRD default (512).",
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


# =============================================================================
# Health & warm-up — Pod-mirrored spec.startupProbe (core/v1 Probe) and
# spec.lifecycle.postStart (core/v1 LifecycleHandler). See
# docs/rfc-declarative-probes-lifecycle.md §3. Every field is optional; when a
# create request omits ``startupProbe`` / ``lifecycle`` the backend CR builder
# fills the pk-sandbox-base execd defaults (§6), so existing callers are
# unaffected (``exclude_none=True`` drops the omitted fields entirely).
#
# The one documented deviation from stock Pod ``HTTPGetAction`` is the
# ``method`` + ``body`` superset on ``httpGet`` (default method GET) so a warm-up
# can POST execd's ``/code``. Field names mirror the pkui backend models
# (``modules/sandboxv2/models.py``) one-for-one so the serialized JSON is the
# exact shape the backend accepts.
# =============================================================================


class HTTPHeader(BaseModel):
    """One ``httpGet.httpHeaders`` entry (core/v1 HTTPHeader)."""

    name: str = Field(..., min_length=1, description="Header name.")
    value: str = Field(default="", description="Header value.")


class HTTPGetAction(BaseModel):
    """core/v1 HTTPGetAction + the ``method``/``body`` superset (RFC §3)."""

    # Accept both snake_case (Python) and camelCase (CR-shaped dict) input, and
    # serialize to the CRD camelCase keys via ``model_dump(by_alias=True)``.
    model_config = ConfigDict(populate_by_name=True)

    port: int = Field(..., description="Guest port to probe/hit.")
    path: str | None = Field(default=None, description="Request path.")
    host: str | None = Field(default=None, description="Host header override.")
    scheme: str | None = Field(default=None, description="HTTP | HTTPS.")
    http_headers: list[HTTPHeader] | None = Field(
        default=None,
        alias="httpHeaders",
        description="Custom request headers.",
    )
    # Superset over stock Pod HTTPGetAction (documented deviation): lets a warm-up
    # POST a body (e.g. execd /code). Default method is GET when omitted.
    method: str | None = Field(
        default=None, description="HTTP method (superset; default GET)."
    )
    body: str | None = Field(
        default=None, description="Request body (superset; used with method POST)."
    )


class TCPSocketAction(BaseModel):
    """core/v1 TCPSocketAction — a host-side tcp-connect probe."""

    port: int = Field(..., description="Guest port to connect to.")
    host: str | None = Field(default=None, description="Host to connect to.")


class ExecAction(BaseModel):
    """core/v1 ExecAction — run a command inside the guest (vsock agent)."""

    command: list[str] = Field(
        ..., min_length=1, description="argv to run inside the guest."
    )


def _handler_count(
    http_get: HTTPGetAction | None,
    tcp_socket: TCPSocketAction | None,
    exec_action: ExecAction | None,
) -> int:
    return sum(1 for h in (http_get, tcp_socket, exec_action) if h is not None)


class Probe(BaseModel):
    """core/v1 Probe — exactly one of ``httpGet`` / ``tcpSocket`` / ``exec``.

    Serializes to the CRD ``spec.startupProbe`` shape (camelCase handler keys and
    ``failureThreshold`` etc. via ``model_dump(by_alias=True)``). Accepts both
    snake_case and camelCase (CR-shaped dict) input.
    """

    model_config = ConfigDict(populate_by_name=True)

    http_get: HTTPGetAction | None = Field(default=None, alias="httpGet")
    tcp_socket: TCPSocketAction | None = Field(default=None, alias="tcpSocket")
    exec: ExecAction | None = Field(default=None)
    initial_delay_seconds: int | None = Field(
        default=None, ge=0, alias="initialDelaySeconds"
    )
    period_seconds: int | None = Field(default=None, ge=1, alias="periodSeconds")
    timeout_seconds: int | None = Field(default=None, ge=1, alias="timeoutSeconds")
    failure_threshold: int | None = Field(default=None, ge=1, alias="failureThreshold")
    success_threshold: int | None = Field(default=None, ge=1, alias="successThreshold")

    @model_validator(mode="after")
    def _exactly_one_handler(self) -> Probe:
        n = _handler_count(self.http_get, self.tcp_socket, self.exec)
        if n != 1:
            raise ValueError(
                "startupProbe must set exactly one handler "
                f"(httpGet | tcpSocket | exec), got {n}"
            )
        return self


class LifecycleHandler(BaseModel):
    """core/v1 LifecycleHandler — exactly one of ``httpGet`` / ``tcpSocket`` /
    ``exec``. Serializes to the CRD ``lifecycle.postStart`` shape."""

    model_config = ConfigDict(populate_by_name=True)

    http_get: HTTPGetAction | None = Field(default=None, alias="httpGet")
    tcp_socket: TCPSocketAction | None = Field(default=None, alias="tcpSocket")
    exec: ExecAction | None = Field(default=None)

    @model_validator(mode="after")
    def _exactly_one_handler(self) -> LifecycleHandler:
        n = _handler_count(self.http_get, self.tcp_socket, self.exec)
        if n != 1:
            raise ValueError(
                "lifecycle.postStart must set exactly one handler "
                f"(httpGet | tcpSocket | exec), got {n}"
            )
        return self


class Lifecycle(BaseModel):
    """core/v1 Lifecycle — only ``postStart`` is modelled (RFC §3 subset)."""

    model_config = ConfigDict(populate_by_name=True)

    post_start: LifecycleHandler | None = Field(default=None, alias="postStart")


# =============================================================================
# Guest DNS — Pod-mirrored spec.dnsPolicy + spec.dnsConfig. The executor writes
# the guest ``/etc/resolv.conf`` host-side at COLD BOOT (the way a container
# runtime does), mirroring the Pod spec. Every field is optional; when a create
# request omits ``dnsPolicy`` / ``dnsConfig`` the executor applies its
# ClusterFirst default (mirror the pod resolver + append 1.1.1.1), so existing
# callers are unaffected (``exclude_none=True`` drops the omitted fields).
# Field names mirror the FirecrackerSandbox CRD / Pod spec one-for-one so the
# serialized JSON is the exact shape the backend accepts. See
# docs/sandbox-dns-design.md.
# =============================================================================


class DNSConfigOption(BaseModel):
    """One ``dnsConfig.options`` entry (Pod PodDNSConfigOption).

    Rendered guest-side as ``name`` or ``name:value``. ``value`` is optional.
    """

    name: str = Field(..., min_length=1, description="Option name, e.g. ndots.")
    value: str | None = Field(
        default=None, description='Optional option value, e.g. "5" for ndots.'
    )


class DNSConfig(BaseModel):
    """core/v1 PodDNSConfig — resolver config merged into the guest resolv.conf.

    With ``dnsPolicy: None`` it is the ENTIRE config; with ``ClusterFirst`` it
    augments the mirrored base (nameservers appended, searches merged, options
    set by name — K8s merge semantics). Accepts both snake_case (Python) and
    camelCase (CR-shaped dict) input; serializes to the CRD shape via
    ``model_dump(by_alias=True)``.
    """

    model_config = ConfigDict(populate_by_name=True)

    nameservers: list[str] | None = Field(
        default=None, description="Resolver IPs appended to the base nameservers."
    )
    searches: list[str] | None = Field(
        default=None, description="DNS search domains merged into the base list."
    )
    options: list[DNSConfigOption] | None = Field(
        default=None, description="Resolver options set by name."
    )


class CreateSandboxV2Request(BaseModel):
    """Request body for ``POST .../sandboxv2`` (mirrors backend model)."""

    name: str = Field(..., min_length=1, max_length=63, description="Sandbox name")
    image: str | None = Field(
        default=None,
        description="Base OCI image (defaults to pk-sandbox-base). Mutually "
        "exclusive with template.",
    )
    template: str | None = Field(
        default=None,
        description="Name of an existing FirecrackerTemplate to resume-clone "
        "from (spec.template), instead of building from an OCI "
        "image. Mutually exclusive with image; when both are omitted image "
        "defaults to pk-sandbox-base.",
    )
    vcpus: int | None = Field(default=None, ge=1, description="Guest vCPUs")
    mem_mib: int | None = Field(
        default=None, ge=128, serialization_alias="memMiB", description="Guest MiB"
    )
    overlay_mib: int | None = Field(
        default=None,
        ge=64,
        le=262144,
        serialization_alias="overlayMiB",
        description="Writable-overlay (rootfs scratch) cap in MiB. Sparse -> a "
        "ceiling, not a reservation. Omitted -> CRD default (512).",
    )
    terminal: bool = Field(
        default=True, description="Inject a ttyd Terminal (:7681) into the guest"
    )
    image_pull_secrets: list[str] | None = Field(
        default=None,
        serialization_alias="imagePullSecrets",
        description="Registry pull secret names",
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
    startup_probe: Probe | None = Field(
        default=None,
        serialization_alias="startupProbe",
        description="spec.startupProbe (core/v1 Probe) gating boot-readiness. "
        "Omitted -> the backend fills the pk-sandbox-base execd default "
        "(httpGet /ping). See docs/rfc-declarative-probes-lifecycle.md §3/§6.",
    )
    lifecycle: Lifecycle | None = Field(
        default=None,
        description="spec.lifecycle (core/v1 Lifecycle; only postStart modelled) "
        "— a one-shot warm-up run after the probe passes (baked into a pool "
        "member's snapshot). Omitted -> the execd /code POST default.",
    )
    dns_policy: str | None = Field(
        default=None,
        serialization_alias="dnsPolicy",
        description="spec.dnsPolicy (ClusterFirst | None | Default) — how the "
        "guest /etc/resolv.conf is written at cold boot. Omitted -> the executor "
        "ClusterFirst default (mirror the pod resolver + 1.1.1.1 fallback).",
    )
    dns_config: DNSConfig | None = Field(
        default=None,
        serialization_alias="dnsConfig",
        description="spec.dnsConfig (Pod PodDNSConfig) — extra resolver config "
        "merged into the guest resolv.conf (nameservers/searches/options).",
    )
    mesh: bool | None = Field(
        default=None,
        description="Optional: opt this sandbox into the Istio service mesh "
        "(spec.mesh).",
    )
    snapshot_resume_policy: str | None = Field(
        default=None,
        serialization_alias="snapshotResumePolicy",
        description="spec.snapshotResumePolicy (Strict | AllowStale) — whether "
        "resuming from a pool member's snapshot requires an exact recipe/base "
        "match. Omitted -> the executor Strict default.",
    )
    manifest: dict[str, Any] | None = Field(
        default=None,
        description="Full FirecrackerSandbox object; wins over structured knobs",
    )

    @model_validator(mode="after")
    def _image_xor_template(self) -> CreateSandboxV2Request:
        if self.manifest is not None:
            return self
        if self.image is not None and self.template is not None:
            raise ValueError(
                "image and template are mutually exclusive — set at most one "
                "(image for an OCI ref, template to resume-clone an existing "
                "FirecrackerTemplate by name)"
            )
        return self

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

    Matches the backend V2 exec contract field names verbatim (snake_case on the
    wire — the v2 exec endpoint does not use camelCase aliases).

    ``stateful`` is the V2 contract flag (replaces the Jupyter-era
    ``use_jupyter``): ``true`` routes to the persistent per-language session
    (guest agent ``/code`` path — survives hibernate/resume); ``false`` runs
    one-shot stateless via the guest ``/command`` path. ``reset_session`` and
    ``session_id`` are only meaningful when ``stateful`` is true.
    """

    code: str = Field(..., description="Code or command to execute")
    language: str = Field(
        default="bash",
        description="'bash' for raw commands, 'python'/'node' for code",
    )
    timeout: int = Field(default=60, ge=1, le=300, description="Timeout in seconds")
    workdir: str = Field(default="/workspace", description="Working directory")
    stateful: bool = Field(
        default=False,
        description="Route to the persistent per-language session (true) or "
        "one-shot stateless execution (false)",
    )
    session_id: str | None = Field(
        default=None, description="Session ID for the stateful session"
    )
    reset_session: bool = Field(
        default=False,
        description="Restart the language child before executing (stateful only)",
    )


class UploadFileV2Request(BaseModel):
    """Request body for ``POST .../sandboxv2/{name}/files``."""

    path: str = Field(..., description="Destination path in the guest")
    content: str = Field(..., description="File content encoded per ``encoding``")
    encoding: str = Field(
        default="base64", description="Content encoding ('text' or 'base64')"
    )


# =============================================================================
# Templates — capture a RUNNING sandbox into a reusable FirecrackerTemplate,
# and resume-clone from one on a later create (via ``template``, see
# :class:`CreateSandboxV2Request`). Mirrors the pkui backend
# ``modules/sandboxv2`` template DTOs (MakeTemplateRequest / Template /
# TemplateListResponse). Capture is ASYNC: the create response confirms the
# request was accepted, not that the template is Ready — the sandbox keeps
# running throughout; poll :meth:`SandboxV2Client.templates` for
# ``phase == "Ready"``.
#
# Endpoints:
#   POST .../sandboxv2/{name}/template  -> Template (201; a sub-path of the
#       sandbox, not a sibling collection). Always NAMESPACE-scoped.
#   GET  .../sandboxv2-templates        -> {templates: [Template], total}
#       (best-effort — empty list if unavailable). MERGED view: namespaced
#       FirecrackerTemplates + cluster-scoped FirecrackerClusterTemplates,
#       each entry tagged with ``scope`` (server resolves shadowing).
# =============================================================================


class MakeTemplateRequest(BaseModel):
    """Request body for ``POST .../sandboxv2/{name}/template``."""

    name: str = Field(..., min_length=1, max_length=63, description="Template name")


class Template(BaseModel):
    """A FirecrackerTemplate (``Template`` in the backend contract).

    Returned both by the live-capture POST (the newly-created, always
    namespace-scoped template) and the ``sandboxv2-templates`` list endpoint
    (a MERGED view of namespaced FirecrackerTemplates + cluster-scoped
    FirecrackerClusterTemplates). ``scope`` says which kind an entry came
    from; for a namespace template ``namespace`` is ``metadata.namespace``,
    and it is ``None`` for a cluster template. ``phase`` reaches ``"Ready"``
    once the async capture completes; it may be ``None`` right after creation,
    before status is populated.
    """

    name: str = Field(..., description="FirecrackerTemplate (CR) name")
    scope: Literal["namespace", "cluster"] = Field(
        ...,
        description="Resolution scope of this template — 'namespace' (a "
        "namespaced FirecrackerTemplate) or 'cluster' (a cluster-scoped "
        "FirecrackerClusterTemplate). Set by the merged listing endpoint; "
        "shadowing (a namespace template hiding a same-named cluster one) is "
        "resolved server-side.",
    )
    namespace: str | None = Field(
        default=None,
        description="FirecrackerTemplate namespace (None for cluster-scoped)",
    )
    phase: str | None = Field(
        default=None,
        description="FirecrackerTemplate status.phase (Pending | Building | "
        "Ready | Failed), or None before status is populated",
    )
    from_sandbox: str | None = Field(
        default=None, description="spec.snapshotOf.fromSandbox — source sandbox name"
    )
    snapshot_id: str | None = Field(
        default=None, description="status.snapshotId, if populated"
    )
    message: str | None = Field(
        default=None, description="status.message, if any (e.g. failure detail)"
    )
    created_at: str | None = Field(
        default=None, description="metadata.creationTimestamp, verbatim"
    )
