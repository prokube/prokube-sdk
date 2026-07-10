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
    FileWriteRequest,
)
from prokube.sandboxv2.models import (
    CodeResult,
    CommandResult,
    CreateSandboxV2Request,
    DNSConfig,
    ExecV2Request,
    FileInfo,
    Lifecycle,
    Probe,
    SandboxV2Info,
    SandboxV2Status,
    SnapshotInfo,
    SnapshotSandboxRequest,
    UploadFileV2Request,
)

if TYPE_CHECKING:
    from prokube.common.config import Config


def _parse_status(status_str: str | None, default: SandboxV2Status) -> SandboxV2Status:
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
    # ``SandboxClient._sandboxes_path``:
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

    # -- parsing --------------------------------------------------------------

    def _parse_info(self, response: dict[str, object]) -> SandboxV2Info:
        return SandboxV2Info(
            name=response.get("name", ""),
            workspace=response.get("namespace", self._workspace()),
            status=_parse_status(response.get("phase"), SandboxV2Status.UNKNOWN),
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
        mesh: bool | None = None,
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

        ``mesh`` is Optional: opt this sandbox into the Istio service mesh
        (spec.mesh).

        ``snapshot_resume_policy`` (spec.snapshotResumePolicy: Strict |
        AllowStale) controls whether resuming from a snapshot image requires an
        exact recipe/base match. Omitted -> the executor Strict default, so
        existing callers are unaffected.

        ``image`` is always an OCI ref (or omitted for the backend default);
        the backend has no by-name knob for launching from a snapshot
        FirecrackerImage (created via :meth:`snapshot`) — that goes through
        ``manifest.spec.firecrackerImage.name`` instead. See
        :meth:`prokube.sandboxv2.sandbox.SandboxV2.from_snapshot`.
        """
        if name is None:
            name = f"sandbox-{uuid.uuid4().hex[:8]}"

        request = CreateSandboxV2Request(
            name=name,
            image=image,
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
            mesh=mesh,
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

    def snapshot(self, name: str, snapshot_name: str) -> SnapshotInfo:
        """Snapshot a RUNNING sandbox into a reusable FirecrackerImage.

        POSTs to ``.../sandboxv2/{name}/snapshot``. The backend creates a
        FirecrackerImage named ``snapshot_name`` and captures the microVM into
        it ASYNCHRONOUSLY — this call returns as soon as the capture is
        accepted, not once the image is ``Ready``; the sandbox keeps running
        throughout. Launch a new sandbox from the (eventually Ready) image via
        :meth:`prokube.sandboxv2.sandbox.SandboxV2.from_snapshot`.

        Args:
            name: Name of the running sandbox to snapshot.
            snapshot_name: Name for the new snapshot FirecrackerImage.

        Raises:
            NotFoundError: If the sandbox does not exist (HTTP 404).
            SandboxError: If the sandbox is not Running (HTTP 409).
            ProKubeError: If the FirecrackerSandbox/FirecrackerImage CRDs are
                not installed (HTTP 503), or any other backend error.
        """
        request = SnapshotSandboxRequest(name=snapshot_name)
        try:
            response = self._http.post(
                self._sandbox_sub_path(name, "snapshot"),
                json=request.model_dump(),
            )
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise
        return SnapshotInfo(
            image=response.get("image", snapshot_name),
            sandbox=response.get("sandbox", name),
        )

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
        """Execute code in the persistent per-language session (stateful)."""
        request = ExecV2Request(
            code=code,
            language=language,
            timeout=min(timeout, 300),
            stateful=True,
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
        """Execute a shell command in the guest (one-shot, stateless).

        v2's ``/exec`` endpoint runs a raw shell command when ``stateful`` is
        False and ``language`` is ``bash`` — no separate command endpoint
        exists, so shell commands ride the same endpoint as code with those
        settings (backend routes ``stateful=false`` to the stateless
        ``/command`` path).
        """
        request = ExecV2Request(
            code=command,
            language="bash",
            timeout=min(timeout, 300),
            stateful=False,
        )
        response = self._http.post(
            self._sandbox_sub_path(name, "exec"),
            json=request.model_dump(exclude={"session_id", "reset_session"}),
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
