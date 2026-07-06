"""HTTP client for Sandbox v2 (Firecracker) API operations.

Reuses ``prokube.common`` verbatim (same ``x-api-key`` auth, http client,
config, exceptions) and the v1 execd result models. Only the path space and the
create/get/pause/resume lifecycle differ from v1:

* v1 paths:  ``/sandbox/{ws}/sandboxes/...`` (external / api-key) or
  ``/_platform/sandbox/{ws}/sandboxes/...`` (in-cluster).
* v2 paths mirror the same api-key vs in-cluster branch:
  - api-key (external ORIGIN route): ``/sandboxv2/{ws}/sandboxes/...`` —
    top-level on the ingress gateway (no ``/pkui`` prefix, no ``/api``), because
    the shared :class:`HttpClient` strips ``api_url`` to its origin under an
    api key. This exactly mirrors v1's external ``/sandbox/{ws}/...`` routes.
  - in-cluster: ``/api/namespaces/{ws}/sandboxv2/...`` — the FastAPI router is
    mounted at ``/api`` and is namespaced.

``workspace`` is the v1 name for the Kubernetes namespace; the two are the same
thing and it is used verbatim in every v2 path.

The exposed method surface (``exec_code`` / ``exec_command`` / ``write_file`` /
``read_file`` / ``list_files`` / ``write_files_batch``) matches v1's
``SandboxClient`` exactly so the v1 ``CodeRunner`` / ``CommandRunner`` /
``FileManager`` helpers can be reused unmodified.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from prokube.common.compat import check_backend_compatibility
from prokube.common.exceptions import NotFoundError, ProKubeError, SandboxError
from prokube.common.http import HttpClient
from prokube.sandbox.client import _parse_batch_file_write_response
from prokube.sandbox.models import (
    BatchFileWriteRequest,
    BatchFileWriteResponse,
    ClaimRequest,
    FileWriteRequest,
)
from prokube.sandboxv2.models import (
    CodeResult,
    CommandResult,
    CreateHibernatedPoolRequest,
    CreateSandboxV2Request,
    DNSConfig,
    ExecV2Request,
    FileInfo,
    HibernatedPoolInfo,
    HibernatedPoolMember,
    Lifecycle,
    Probe,
    SandboxV2Info,
    SandboxV2Status,
    UpdatePoolRequest,
    UploadFileV2Request,
)

if TYPE_CHECKING:
    from prokube.common.config import Config


def _parse_status(
    status_str: str | None, default: SandboxV2Status
) -> SandboxV2Status:
    """Parse a phase string to SandboxV2Status, tolerating unknown values."""
    if not status_str:
        return default
    try:
        return SandboxV2Status(status_str)
    except ValueError:
        return SandboxV2Status.UNKNOWN


class SandboxV2Client:
    """Client for Sandbox v2 (Firecracker) API operations."""

    def __init__(self, config: Config, check_version: bool = True) -> None:
        """Initialize the v2 sandbox client.

        Args:
            config: SDK configuration. ``config.workspace`` is the workspace
                (Kubernetes namespace) used in every v2 path.
            check_version: Whether to check backend version compatibility.
        """
        self.config = config
        self._http = HttpClient(config)

        if check_version:
            check_backend_compatibility(self._http)

    def close(self) -> None:
        """Close the client."""
        self._http.close()

    # -- path helpers ---------------------------------------------------------
    #
    # Every path method branches on ``use_api_key`` exactly like v1's
    # ``SandboxClient._sandboxes_path`` / ``PoolClient._pools_path``:
    #
    # * api-key -> top-level ORIGIN routes (``/sandboxv2/{ws}/...``). The shared
    #   ``HttpClient`` uses only the ``api_url`` origin under an api key, so NO
    #   ``/pkui`` prefix and NO ``/api`` segment may be attached here.
    # * in-cluster -> namespaced FastAPI routes (``/api/namespaces/{ws}/...``).

    def _workspace(self) -> str:
        return self.config.workspace

    def _collection_path(self) -> str:
        ws = self._workspace()
        if self.config.use_api_key:
            return f"/sandboxv2/{ws}/sandboxes"
        return f"/api/namespaces/{ws}/sandboxv2"

    def _sandbox_path(self, name: str) -> str:
        return f"{self._collection_path()}/{name}"

    def _sandbox_sub_path(self, name: str, sub: str) -> str:
        return f"{self._sandbox_path(name)}/{sub}"

    def _claim_path(self) -> str:
        """POST target for claiming a warm-pool member.

        Under api-key this is the ORIGIN route ``/sandboxv2/{ws}/sandboxes/claim``
        (pool name travels in the JSON body, exactly as v1's
        ``claim_from_pool``). In-cluster claims instead POST to
        ``.../sandboxv2-pools/{pool}/claim`` (pool name in the URL); see
        :meth:`claim`.
        """
        return f"{self._collection_path()}/claim"

    def _pools_path(self) -> str:
        ws = self._workspace()
        if self.config.use_api_key:
            # Pool CRUD is not part of the deployed api-key ORIGIN contract
            # (only pool *claim* is exposed, via the sandboxes/claim route
            # above). Provide a consistent top-level path for completeness.
            return f"/sandboxv2/{ws}/pools"
        # In-cluster: sibling collection of ``sandboxv2`` (``sandboxv2-pools``),
        # NOT a sub-path of it — mirrors the pkui backend route registration.
        return f"/api/namespaces/{ws}/sandboxv2-pools"

    def _pool_path(self, name: str) -> str:
        return f"{self._pools_path()}/{name}"

    # -- parsing --------------------------------------------------------------

    def _parse_info(self, response: dict[str, object]) -> SandboxV2Info:
        return SandboxV2Info(
            name=response.get("name", ""),
            workspace=response.get("namespace", self._workspace()),
            status=_parse_status(
                response.get("phase"), SandboxV2Status.UNKNOWN
            ),
            image=response.get("image") or None,
            runtime_class=response.get("runtimeClassName"),
            operating_mode=response.get("operatingMode") or None,
            node=response.get("node"),
            endpoint=response.get("endpoint"),
            terminal_enabled=bool(response.get("terminalEnabled", True)),
            message=response.get("message"),
            created_at=response.get("createdAt"),
        )

    # -- lifecycle ------------------------------------------------------------

    def create(
        self,
        image: str | None = None,
        name: str | None = None,
        runtime_class: str = "fc-host",
        vcpus: int | None = None,
        mem_mib: int | None = None,
        egress: bool = False,
        terminal: bool = True,
        env_vars: dict[str, str] | list[dict[str, str]] | None = None,
        secret_refs: list[str] | None = None,
        volumes: list[dict[str, object]] | None = None,
        volume_mounts: list[dict[str, object]] | None = None,
        image_pull_secrets: list[str] | None = None,
        workspace_size: str | None = None,
        target_node: str | None = None,
        operating_mode: str | None = None,
        startup_probe: Probe | dict[str, object] | None = None,
        lifecycle: Lifecycle | dict[str, object] | None = None,
        dns_policy: str | None = None,
        dns_config: DNSConfig | dict[str, object] | None = None,
        snapshot_resume_policy: str | None = None,
        manifest: dict[str, object] | None = None,
    ) -> SandboxV2Info:
        """Create a new Firecracker sandbox.

        Returns as soon as the CR is created; the controller drives
        Pending -> Running asynchronously (poll via :meth:`get`).

        ``env_vars`` accepts a ``dict[str,str]`` or a list of ``{name,value}``
        dicts and serializes to CRD ``spec.env``; ``secret_refs`` accepts a list
        of Secret names and serializes to CRD ``spec.envFrom``. Env is baked into
        the guest at boot/snapshot and is not refreshed on pause/resume.

        ``startup_probe`` (spec.startupProbe, core/v1 Probe) and ``lifecycle``
        (spec.lifecycle with ``postStart``, core/v1 Lifecycle) are Pod-mirrored
        readiness/warm-up knobs. Each accepts a model instance or a CR-shaped
        dict. Omitted -> the backend fills the pk-sandbox-base execd defaults, so
        existing callers are unaffected.

        ``dns_policy`` (spec.dnsPolicy: ClusterFirst | None | Default) and
        ``dns_config`` (spec.dnsConfig, Pod-mirrored nameservers/searches/options)
        control the guest ``/etc/resolv.conf`` written host-side at cold boot.
        ``dns_config`` accepts a model instance or a CR-shaped dict. Omitted ->
        the executor ClusterFirst default, so existing callers are unaffected.

        ``snapshot_resume_policy`` (spec.snapshotResumePolicy: Strict |
        AllowStale) controls whether resuming from a pool member's snapshot
        requires an exact recipe/base match. Omitted -> the executor Strict
        default, so existing callers are unaffected.
        """
        if name is None:
            name = f"sandbox-{uuid.uuid4().hex[:8]}"

        request = CreateSandboxV2Request(
            name=name,
            image=image,
            runtime_class_name=runtime_class,
            vcpus=vcpus,
            mem_mib=mem_mib,
            egress=egress,
            terminal=terminal,
            env=env_vars,
            env_from=secret_refs,
            volumes=volumes,
            volume_mounts=volume_mounts,
            image_pull_secrets=image_pull_secrets,
            workspace_size=workspace_size,
            target_node=target_node,
            operating_mode=operating_mode,
            startup_probe=startup_probe,
            lifecycle=lifecycle,
            dns_policy=dns_policy,
            dns_config=dns_config,
            snapshot_resume_policy=snapshot_resume_policy,
            manifest=manifest,
        )
        response = self._http.post(
            self._collection_path(),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        info = self._parse_info(response)
        # Backend may briefly report Unknown/empty phase right after create;
        # default to Pending so callers can wait on it.
        if info.status == SandboxV2Status.UNKNOWN:
            info.status = SandboxV2Status.PENDING
        return info

    def get(self, name: str) -> SandboxV2Info:
        """Get information about a sandbox."""
        response = self._http.get(self._sandbox_path(name))
        return self._parse_info(response)

    def wait_ready(self, name: str, timeout: int = 30) -> SandboxV2Info:
        """Server-side long-poll for readiness (single request).

        The backend tight-polls committed CR state in-cluster and returns the
        moment ``status.phase`` reaches Running (or Failed), or the latest phase
        when its ``timeout`` elapses. This collapses the old flat client poll
        (and its per-poll internet round-trip) into one request resolved near the
        control-plane. Raises :class:`~prokube.common.exceptions.NotFoundError`
        when the endpoint is absent (older backend) or the sandbox is missing —
        the caller then falls back to a local ``get`` poll.
        """
        response = self._http.get(
            self._sandbox_sub_path(name, "wait_ready"),
            params={"timeout": timeout},
            # Give httpx headroom over the server-side long-poll window.
            timeout=timeout + 10,
        )
        return self._parse_info(response)

    def list(self) -> list[SandboxV2Info]:
        """List all Firecracker sandboxes in the configured workspace."""
        response = self._http.get(self._collection_path())
        sandboxes = response.get("sandboxes", [])
        return [self._parse_info(s) for s in sandboxes]

    def pause(self, name: str) -> SandboxV2Info:
        """Pause a running sandbox (native VM snapshot -> Hibernated)."""
        try:
            response = self._http.post(self._sandbox_sub_path(name, "pause"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise
        return self._parse_info(response)

    def resume(self, name: str) -> SandboxV2Info:
        """Resume a paused sandbox (native VM restore -> Running)."""
        try:
            response = self._http.post(self._sandbox_sub_path(name, "resume"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise
        return self._parse_info(response)

    def delete(self, name: str) -> None:
        """Delete a sandbox (deletes the CR; ephemeral PVC cleaned up)."""
        self._http.delete(self._sandbox_path(name))

    # -- pools (FirecrackerHibernatedPool) ------------------------------------

    def _parse_pool(self, response: dict[str, object]) -> HibernatedPoolInfo:
        members = [
            HibernatedPoolMember(name=m.get("name", ""), phase=m.get("phase"))
            for m in (response.get("members") or [])
            if m.get("name")
        ]
        return HibernatedPoolInfo(
            name=response.get("name", ""),
            workspace=response.get("namespace", self._workspace()),
            size=response.get("size", 0) or 0,
            warm_state=response.get("warmState") or "Hibernated",
            ready_members=response.get("readyMembers", 0) or 0,
            members=members,
            image=response.get("image") or None,
            runtime_class_name=response.get("runtimeClassName"),
            message=response.get("message"),
            created_at=response.get("createdAt"),
        )

    def create_pool(
        self,
        name: str,
        size: int,
        template: CreateSandboxV2Request,
        warm_state: str = "Hibernated",
    ) -> HibernatedPoolInfo:
        """Create a warm pool of Firecracker sandboxes.

        Args:
            name: Pool name.
            size: Desired number of warm members.
            template: The v2 sandbox create spec used as the member template
                (same knobs as :meth:`create`). The backend forces the template
                to ``runtimeClassName: fc-host`` and owns ``operatingMode``.
            warm_state: How warm members are kept — ``"Hibernated"`` (default,
                pre-snapshotted; a claim is a fast resume) or ``"Running"``
                (members kept hot). Editable later via :meth:`set_pool_warm_state`.
        """
        request = CreateHibernatedPoolRequest(
            name=name, size=size, warm_state=warm_state, template=template
        )
        response = self._http.post(
            self._pools_path(),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        return self._parse_pool(response)

    def set_pool_warm_state(
        self, name: str, warm_state: str
    ) -> HibernatedPoolInfo:
        """Change a pool's ``warmState`` post-create (Hibernated <-> Running).

        The fc controller reconciles every member to the new state.

        Args:
            name: Pool name.
            warm_state: ``"Hibernated"`` or ``"Running"``.
        """
        request = UpdatePoolRequest(warm_state=warm_state)
        response = self._http.patch(
            self._pool_path(name),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        return self._parse_pool(response)

    def list_pools(self) -> list[HibernatedPoolInfo]:
        """List all warm pools in the configured workspace."""
        response = self._http.get(self._pools_path())
        pools = response.get("pools", [])
        return [self._parse_pool(p) for p in pools]

    def get_pool(self, name: str) -> HibernatedPoolInfo:
        """Get information about a warm pool."""
        response = self._http.get(self._pool_path(name))
        return self._parse_pool(response)

    def delete_pool(self, name: str) -> None:
        """Delete a warm pool (the controller garbage-collects its members)."""
        self._http.delete(self._pool_path(name))

    def claim(
        self, name: str, auto_idle_timeout_seconds: int | None = None
    ) -> SandboxV2Info:
        """Claim a ready member from a warm pool (fast resume, not cold boot).

        Returns the now-resuming sandbox, detached from the pool. The pool
        controller refills to keep ``spec.size`` warm.

        Path/body mirror v1's ``claim_from_pool`` under api-key:

        * api-key -> ``POST /sandboxv2/{ws}/sandboxes/claim`` with the pool name
          in the JSON body (v1's :class:`ClaimRequest` shape: ``poolName`` +
          optional ``autoIdleTimeoutSeconds``).
        * in-cluster -> ``POST .../sandboxv2-pools/{pool}/claim`` (pool name in
          the URL, no body — the existing warm-pool controller contract).

        Args:
            name: Name of the warm pool to claim from.
            auto_idle_timeout_seconds: Per-claim auto-idle override in seconds
                (only forwarded on the api-key origin route).

        Raises:
            SandboxError: If no warm member is ready to claim (HTTP 409).
        """
        try:
            if self.config.use_api_key:
                request = ClaimRequest(
                    pool_name=name,
                    auto_idle_timeout_seconds=auto_idle_timeout_seconds,
                )
                response = self._http.post(
                    self._claim_path(),
                    json=request.model_dump(by_alias=True, exclude_none=True),
                )
            else:
                response = self._http.post(f"{self._pool_path(name)}/claim")
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise
        return self._parse_info(response)

    # -- exec -----------------------------------------------------------------

    def exec_code(
        self,
        name: str,
        code: str,
        language: str = "python",
        timeout: int = 300,
        session_id: str | None = None,
        reset_session: bool = False,
    ) -> CodeResult:
        """Execute code in the guest Jupyter kernel (stateful)."""
        request = ExecV2Request(
            code=code,
            language=language,
            timeout=min(timeout, 300),
            use_jupyter=True,
            session_id=session_id,
            reset_session=reset_session,
        )
        response = self._http.post(
            self._sandbox_sub_path(name, "exec"),
            json=request.model_dump(exclude_none=True),
        )
        exit_code = response.get("exitCode", 0)
        success = response.get("success")
        if success is None:
            success = exit_code == 0
        return CodeResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            success=bool(success),
            execution_time_ms=response.get("durationMs", 0),
            session_id=response.get("session_id"),
        )

    def exec_command(
        self,
        name: str,
        command: str,
        timeout: int = 300,
    ) -> CommandResult:
        """Execute a shell command in the guest.

        v2's ``/exec`` endpoint runs a raw shell command when ``use_jupyter`` is
        False and ``language`` is ``bash`` — no separate command endpoint
        exists, so shell commands ride the same endpoint as code with those
        settings.
        """
        request = ExecV2Request(
            code=command,
            language="bash",
            timeout=min(timeout, 300),
            use_jupyter=False,
        )
        response = self._http.post(
            self._sandbox_sub_path(name, "exec"),
            json=request.model_dump(
                exclude={"session_id", "reset_session"}
            ),
        )
        return CommandResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            exit_code=response.get("exitCode", -1),
            duration_ms=response.get("durationMs", 0),
        )

    # -- files ----------------------------------------------------------------

    def write_file(self, name: str, path: str, content: bytes) -> None:
        """Write a file to the guest (base64-encoded on the wire)."""
        request = UploadFileV2Request(
            path=path,
            content=base64.b64encode(content).decode("ascii"),
            encoding="base64",
        )
        self._http.post(
            self._sandbox_sub_path(name, "files"),
            json=request.model_dump(),
        )

    def write_files_batch(
        self, name: str, items: Sequence[tuple[str, bytes]]
    ) -> BatchFileWriteResponse:
        """Write multiple files to the guest in one request."""
        request = BatchFileWriteRequest(
            items=[
                FileWriteRequest(
                    path=path,
                    content=base64.b64encode(content).decode("ascii"),
                    encoding="base64",
                )
                for path, content in items
            ]
        )
        try:
            response = self._http.post(
                self._sandbox_sub_path(name, "files/batch"),
                json=request.model_dump(),
            )
        except NotFoundError as e:
            try:
                self.get(name)
            except NotFoundError:
                raise
            raise SandboxError(
                "Batch file writes require a backend that supports the "
                "sandboxv2 /files/batch endpoint",
                status_code=e.status_code,
            ) from e
        except ProKubeError as e:
            if e.status_code == 405:
                raise SandboxError(
                    "Batch file writes require a backend that supports the "
                    "sandboxv2 /files/batch endpoint",
                    status_code=e.status_code,
                ) from e
            raise
        return _parse_batch_file_write_response(response)

    def read_file(self, name: str, path: str) -> bytes:
        """Read a file from the guest."""
        return self._http.get_bytes(
            self._sandbox_sub_path(name, "files/download"),
            params={"path": path},
        )

    def list_files(self, name: str, path: str = "/workspace") -> list[FileInfo]:
        """List files in a guest directory."""
        response = self._http.get(
            self._sandbox_sub_path(name, "files"),
            params={"path": path},
        )
        files = response.get("files", [])
        return [
            FileInfo(
                name=f["name"],
                path=f["path"],
                # v2 backend uses isDirectory/modifiedAt; keep v1 aliases too.
                is_dir=f.get("isDirectory", f.get("is_dir", f.get("isDir", False))),
                size=f.get("size", 0),
                modified=f.get("modifiedAt", f.get("modified")),
            )
            for f in files
        ]
