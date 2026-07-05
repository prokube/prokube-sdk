"""HTTP client for Sandbox v2 (Firecracker) API operations.

Reuses ``prokube.common`` verbatim (same ``x-api-key`` auth, http client,
config, exceptions) and the v1 execd result models. Only the path space and the
create/get/pause/resume lifecycle differ from v1:

* v1 paths:  ``/sandbox/{ws}/sandboxes/...`` (external) or
  ``/_platform/sandbox/{ws}/sandboxes/...`` (in-cluster).
* v2 paths:  ``/api/namespaces/{ns}/sandboxv2/...`` — the FastAPI router is
  mounted at ``/api`` and is namespaced. v1's ``workspace`` concept maps 1:1 to
  the v2 ``namespace``.

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
from urllib.parse import urlparse

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
    ExecV2Request,
    FileInfo,
    SandboxV2Info,
    SandboxV2Status,
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
            config: SDK configuration. ``config.workspace`` is used as the v2
                namespace.
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

    def _namespace(self) -> str:
        return self.config.workspace

    def _prefix(self) -> str:
        """Path prefix to prepend under API-key (external) access.

        The shared ``HttpClient`` strips ``api_url`` down to its origin for
        API-key access (v1's external ``/sandbox/*`` routes are top-level). The
        v2 routes, by contrast, live under the app's own path prefix
        (e.g. ``/pkui/api/namespaces/...``), so when using an API key we must
        re-attach the configured path prefix. For in-cluster access the full
        ``api_url`` (prefix included) is preserved by ``HttpClient`` already, so
        no prefix is added here.
        """
        if self.config.use_api_key:
            return urlparse(self.config.api_url).path.rstrip("/")
        return ""

    def _collection_path(self) -> str:
        return f"{self._prefix()}/api/namespaces/{self._namespace()}/sandboxv2"

    def _sandbox_path(self, name: str) -> str:
        return f"{self._collection_path()}/{name}"

    def _sandbox_sub_path(self, name: str, sub: str) -> str:
        return f"{self._sandbox_path(name)}/{sub}"

    # -- parsing --------------------------------------------------------------

    def _parse_info(self, response: dict[str, object]) -> SandboxV2Info:
        return SandboxV2Info(
            name=response.get("name", ""),
            namespace=response.get("namespace", self._namespace()),
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
        volumes: list[dict[str, object]] | None = None,
        volume_mounts: list[dict[str, object]] | None = None,
        image_pull_secrets: list[str] | None = None,
        workspace_size: str | None = None,
        target_node: str | None = None,
        operating_mode: str | None = None,
        manifest: dict[str, object] | None = None,
    ) -> SandboxV2Info:
        """Create a new Firecracker sandbox.

        Returns as soon as the CR is created; the controller drives
        Pending -> Running asynchronously (poll via :meth:`get`).
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
            volumes=volumes,
            volume_mounts=volume_mounts,
            image_pull_secrets=image_pull_secrets,
            workspace_size=workspace_size,
            target_node=target_node,
            operating_mode=operating_mode,
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

    def list(self) -> list[SandboxV2Info]:
        """List all Firecracker sandboxes in the configured namespace."""
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
