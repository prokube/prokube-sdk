"""HTTP client for sandbox API operations."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import TYPE_CHECKING

from prokube.common.compat import check_backend_compatibility
from prokube.common.exceptions import NotFoundError, ProKubeError, SandboxError
from prokube.common.http import HttpClient
from prokube.sandbox.models import (
    BatchFileWriteRequest,
    BatchFileWriteResponse,
    BatchFileWriteResult,
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


def _parse_batch_file_write_response(
    response: dict[str, object],
) -> BatchFileWriteResponse:
    """Normalize the batch file write response contract."""
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Batch file write response must include a results list")

    results: list[BatchFileWriteResult] = []
    for index, item in enumerate(raw_results):
        if not isinstance(item, dict):
            raise ValueError(
                f"Batch file write response item {index} must be an object"
            )
        results.append(
            BatchFileWriteResult(
                index=_require_batch_result_int(item, "index"),
                path=_require_batch_result_str(item, "path"),
                success=_require_batch_result_bool(item, "success"),
                error=item.get("error"),
            )
        )

    results.sort(key=lambda item: item.index)
    seen_indexes: set[int] = set()
    for item in results:
        if item.index < 0:
            raise ValueError("Batch file write response index must be non-negative")
        if item.index in seen_indexes:
            raise ValueError("Batch file write response indexes must be unique")
        seen_indexes.add(item.index)

    success_count = response.get("successCount", response.get("success_count"))
    failure_count = response.get("failureCount", response.get("failure_count"))

    if success_count is None:
        success_count = sum(1 for item in results if item.success)
    if failure_count is None:
        failure_count = len(results) - success_count

    success = response.get("success")
    if not isinstance(success, bool):
        raise ValueError("Batch file write response must include a boolean success")

    return BatchFileWriteResponse(
        success=success,
        total=int(response.get("total", len(results))),
        success_count=int(success_count),
        failure_count=int(failure_count),
        results=results,
    )


def _require_batch_result_str(item: dict[str, object], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Batch file write response item is missing {field}")
    return value


def _require_batch_result_int(item: dict[str, object], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Batch file write response item is missing {field}")
    return value


def _require_batch_result_bool(item: dict[str, object], field: str) -> bool:
    value = item.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"Batch file write response item is missing {field}")
    return value


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

    def claim_from_pool(self, pool: str) -> SandboxInfo:
        """Claim a sandbox from a warm pool.

        Args:
            pool: Name of the warm pool.

        Returns:
            Information about the claimed sandbox.
        """
        request = ClaimRequest(pool_name=pool)
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
        cpu: str | None = None,
        memory: str | None = None,
        allow_internet_access: bool | None = None,
        env_vars: list[dict[str, str]] | None = None,
        secret_refs: list[str] | None = None,
    ) -> SandboxInfo:
        """Create a new sandbox.

        Args:
            image: Container image to use.
            name: Optional sandbox name (auto-generated if not provided).
            cpu: CPU resource request (e.g. '2'). Backend default used if None.
            memory: Memory resource request (e.g. '4Gi'). Backend default used
                if None.
            allow_internet_access: Whether the sandbox may reach the public
                internet. Backend default used if None.
            env_vars: Environment variables to inject into the sandbox. Each
                entry is a ``{"name": ..., "value": ...}`` dict.
            secret_refs: Names of workspace secrets to mount into the sandbox.

        Returns:
            Information about the created sandbox.
        """
        import uuid

        # Generate name if not provided (backend requires name)
        if name is None:
            name = f"sandbox-{uuid.uuid4().hex[:8]}"

        request = CreateRequest(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            allow_internet_access=allow_internet_access,
            env_vars=env_vars,
            secret_refs=secret_refs,
        )
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

    def resume(self, name: str) -> SandboxInfo:
        """Resume a paused sandbox.

        A new pod starts with the same PVC mounts at /workspace and /home/agent.

        Args:
            name: Sandbox name.

        Raises:
            SandboxError: If sandbox is not in Paused state (HTTP 409).
        """
        try:
            response = self._http.post(self._sandbox_sub_path(name, "resume"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise

        return SandboxInfo(
            name=response.get("name", name),
            workspace=self.config.workspace,
            status=_parse_status(
                response.get("status") or response.get("phase"),
                SandboxStatus.PENDING,
            ),
            image=response.get("image"),
            pool=response.get("poolName") or response.get("pool"),
            created_at=response.get("createdAt") or response.get("created_at"),
            resumed_from_pool=response.get("resumedFromPool", False),
        )

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

    def write_files_batch(
        self, name: str, items: Sequence[tuple[str, bytes]]
    ) -> BatchFileWriteResponse:
        """Write multiple files to a sandbox in one request."""
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
                "sandbox /files/batch endpoint",
                status_code=e.status_code,
            ) from e
        except ProKubeError as e:
            if e.status_code == 405:
                raise SandboxError(
                    "Batch file writes require a backend that supports the "
                    "sandbox /files/batch endpoint",
                    status_code=e.status_code,
                ) from e
            raise
        return _parse_batch_file_write_response(response)

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
