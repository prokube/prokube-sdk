"""HTTP client for sandbox API operations."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from prokube.common.compat import check_backend_compatibility
from prokube.common.exceptions import ProKubeError, SandboxError
from prokube.common.http import HttpClient
from prokube.sandbox.models import (
    ClaimRequest,
    CodeResult,
    CommandResult,
    CreateRequest,
    ExecRequest,
    FileInfo,
    FileWriteRequest,
    SandboxInfo,
    SandboxStatus,
)

if TYPE_CHECKING:
    from prokube.common.config import Config


def _parse_status(status_str: str | None, default: SandboxStatus) -> SandboxStatus:
    """Parse status string to SandboxStatus enum.

    Args:
        status_str: Status string from API response.
        default: Default status to use if status_str is None/empty.

    Returns:
        SandboxStatus enum value. Returns default if status_str is falsy,
        or UNKNOWN if the status string doesn't match any known status.
    """
    if not status_str:
        return default
    try:
        return SandboxStatus(status_str)
    except ValueError:
        return SandboxStatus.UNKNOWN


class SandboxClient:
    """Client for sandbox API operations."""

    def __init__(self, config: Config, check_version: bool = True) -> None:
        """Initialize sandbox client.

        Args:
            config: SDK configuration.
            check_version: Whether to check backend version compatibility.
        """
        self.config = config
        self._http = HttpClient(config)

        if check_version:
            check_backend_compatibility(self._http)

    def close(self) -> None:
        """Close the client."""
        self._http.close()

    def _sandboxes_path(self) -> str:
        """Get API path for the sandboxes collection."""
        ws = self.config.workspace
        if self.config.use_api_key:
            return f"/sandbox/{ws}/sandboxes"
        return f"/api/namespaces/{ws}/sandboxes"

    def _sandbox_path(self, name: str) -> str:
        """Get API path for a specific sandbox."""
        return f"{self._sandboxes_path()}/{name}"

    def _sandbox_sub_path(self, name: str, sub: str) -> str:
        """Get API path for a sandbox sub-resource (exec, files, etc.).

        Uses _sandbox_path (which includes /sandboxes/) for both internal
        and external access to ensure consistent URL structure.
        """
        return f"{self._sandbox_path(name)}/{sub}"

    def claim_from_pool(
        self, pool: str, volume_size: str | None = None
    ) -> SandboxInfo:
        """Claim a sandbox from a warm pool.

        Args:
            pool: Name of the warm pool.
            volume_size: PVC volume size (e.g. '20Gi').

        Returns:
            Information about the claimed sandbox.
        """
        request = ClaimRequest(pool_name=pool, volume_size=volume_size)
        response = self._http.post(
            f"{self._sandboxes_path()}/claim",
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        # API returns sandboxName for claim endpoint
        sandbox_name = response.get("sandboxName") or response["name"]
        return SandboxInfo(
            name=sandbox_name,
            workspace=self.config.workspace,
            status=_parse_status(response.get("status"), SandboxStatus.RUNNING),
            pool=pool,
        )

    def create(
        self,
        image: str,
        name: str | None = None,
        volume_size: str | None = None,
    ) -> SandboxInfo:
        """Create a new sandbox.

        Args:
            image: Container image to use.
            name: Optional sandbox name (auto-generated if not provided).
            volume_size: PVC volume size (e.g. '20Gi').

        Returns:
            Information about the created sandbox.
        """
        import uuid

        # Generate name if not provided (backend requires name)
        if name is None:
            name = f"sandbox-{uuid.uuid4().hex[:8]}"

        request = CreateRequest(image=image, name=name, volume_size=volume_size)
        response = self._http.post(
            self._sandboxes_path(),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        # API returns 'phase' instead of 'status' for sandbox phase
        status_str = response.get("status") or response.get("phase")
        return SandboxInfo(
            name=response["name"],
            workspace=self.config.workspace,
            status=_parse_status(status_str, SandboxStatus.PENDING),
            image=image,
        )

    def list(self) -> list[SandboxInfo]:
        """List all sandboxes in the configured workspace.

        Returns:
            List of sandbox info objects.
        """
        response = self._http.get(
            self._sandboxes_path(),
        )
        sandboxes = response.get("sandboxes", [])
        return [
            SandboxInfo(
                name=s["name"],
                workspace=self.config.workspace,
                status=_parse_status(
                    s.get("status") or s.get("phase"), SandboxStatus.UNKNOWN
                ),
                image=s.get("image") or None,
                pool=s.get("poolName") or s.get("pool"),
                created_at=s.get("createdAt") or s.get("created_at"),
            )
            for s in sandboxes
        ]

    def get(self, name: str) -> SandboxInfo:
        """Get information about a sandbox.

        Args:
            name: Sandbox name.

        Returns:
            Information about the sandbox.
        """
        response = self._http.get(self._sandbox_path(name))
        return SandboxInfo(
            name=response["name"],
            workspace=self.config.workspace,
            status=_parse_status(
                response.get("status") or response.get("phase"),
                SandboxStatus.UNKNOWN,
            ),
            image=response.get("image"),
            pool=response.get("poolName") or response.get("pool"),
            created_at=response.get("createdAt") or response.get("created_at"),
        )

    def pause(self, name: str) -> None:
        """Pause a running sandbox.

        Frees compute resources while preserving /workspace and /home/agent.

        Args:
            name: Sandbox name.

        Raises:
            SandboxError: If sandbox is not in Running state (HTTP 409).
        """
        try:
            self._http.post(self._sandbox_sub_path(name, "pause"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise

    def resume(self, name: str) -> None:
        """Resume a paused sandbox.

        A new pod starts with the same PVC mounts at /workspace and /home/agent.

        Args:
            name: Sandbox name.

        Raises:
            SandboxError: If sandbox is not in Paused state (HTTP 409).
        """
        try:
            self._http.post(self._sandbox_sub_path(name, "resume"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise

    def delete(self, name: str) -> None:
        """Delete a sandbox.

        Args:
            name: Sandbox name.
        """
        self._http.delete(self._sandbox_path(name))

    def exec_code(
        self,
        name: str,
        code: str,
        language: str = "python",
        timeout: int = 300,
        session_id: str | None = None,
        reset_session: bool = False,
    ) -> CodeResult:
        """Execute code in sandbox using Jupyter kernel.

        Args:
            name: Sandbox name.
            code: Code to execute.
            language: Programming language.
            timeout: Timeout in seconds.
            session_id: Session ID for stateful execution (reuse from previous call).
            reset_session: If True, restart the kernel before executing code.

        Returns:
            Code execution result including session_id for subsequent calls.
        """
        request = ExecRequest(
            code=code,
            use_jupyter=True,
            timeout=timeout,
            language=language,
            session_id=session_id,
            reset_session=reset_session,
        )
        # Note: exec endpoint uses snake_case (use_jupyter, session_id, reset_session)
        # unlike other endpoints that use camelCase. Do NOT use by_alias=True here.
        response = self._http.post(
            self._sandbox_sub_path(name, "exec"),
            json=request.model_dump(exclude_none=True),
        )
        return CodeResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            success=response.get("success", False),
            execution_time_ms=response.get(
                "durationMs", response.get("execution_time_ms", 0)
            ),
            error_name=response.get("error_name"),
            error_value=response.get("error_value"),
            traceback=response.get("traceback"),
            session_id=response.get("session_id"),
        )

    def exec_command(
        self,
        name: str,
        command: str,
        timeout: int = 300,
    ) -> CommandResult:
        """Execute shell command in sandbox.

        Args:
            name: Sandbox name.
            command: Shell command to execute.
            timeout: Timeout in seconds.

        Returns:
            Command execution result.
        """
        request = ExecRequest(
            code=command,
            use_jupyter=False,
            timeout=timeout,
        )
        # Exclude Jupyter-specific fields for shell commands
        # (language field triggers Python interpreter in backend)
        response = self._http.post(
            self._sandbox_sub_path(name, "exec"),
            json=request.model_dump(
                exclude={"language", "session_id", "reset_session"}
            ),
        )
        return CommandResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            exit_code=response.get("exitCode", response.get("exit_code", -1)),
            duration_ms=response.get("durationMs", response.get("duration_ms", 0)),
        )

    def write_file(self, name: str, path: str, content: bytes) -> None:
        """Write a file to sandbox.

        Args:
            name: Sandbox name.
            path: Path in sandbox where to write.
            content: File content as bytes.
        """
        request = FileWriteRequest(
            path=path,
            content=base64.b64encode(content).decode("ascii"),
        )
        self._http.post(
            self._sandbox_sub_path(name, "files"),
            json=request.model_dump(),
        )

    def read_file(self, name: str, path: str) -> bytes:
        """Read a file from sandbox.

        Args:
            name: Sandbox name.
            path: Path in sandbox to read.

        Returns:
            File content as bytes.
        """
        return self._http.get_bytes(
            self._sandbox_sub_path(name, "files/download"),
            params={"path": path},
        )

    def list_files(self, name: str, path: str = "/workspace") -> list[FileInfo]:
        """List files in a directory.

        Args:
            name: Sandbox name.
            path: Directory path to list.

        Returns:
            List of file information.
        """
        response = self._http.get(
            self._sandbox_sub_path(name, "files"),
            params={"path": path},
        )
        files = response.get("files", [])
        return [
            FileInfo(
                name=f["name"],
                path=f["path"],
                # Handle both snake_case and camelCase from backend
                is_dir=f.get("is_dir", f.get("isDir", False)),
                size=f.get("size", 0),
                modified=f.get("modified"),
            )
            for f in files
        ]
