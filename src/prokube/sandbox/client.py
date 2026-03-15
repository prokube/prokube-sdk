"""HTTP client for sandbox API operations."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from prokube.common.compat import check_backend_compatibility
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

    def _sandbox_path(self, name: str) -> str:
        """Get API path for a sandbox."""
        return f"/api/namespaces/{self.config.workspace}/sandboxes/{name}"

    def claim_from_pool(self, pool: str) -> SandboxInfo:
        """Claim a sandbox from a warm pool.

        Args:
            pool: Name of the warm pool.

        Returns:
            Information about the claimed sandbox.
        """
        request = ClaimRequest(pool=pool)
        response = self._http.post(
            f"/api/namespaces/{self.config.workspace}/sandboxes/claim",
            json=request.model_dump(),
        )
        return SandboxInfo(
            name=response["name"],
            workspace=self.config.workspace,
            status=SandboxStatus(response.get("status", "Running")),
            pool=pool,
        )

    def create(self, image: str, name: str | None = None) -> SandboxInfo:
        """Create a new sandbox.

        Args:
            image: Container image to use.
            name: Optional sandbox name.

        Returns:
            Information about the created sandbox.
        """
        request = CreateRequest(image=image, name=name)
        response = self._http.post(
            f"/api/namespaces/{self.config.workspace}/sandboxes",
            json=request.model_dump(exclude_none=True),
        )
        return SandboxInfo(
            name=response["name"],
            workspace=self.config.workspace,
            status=SandboxStatus(response.get("status", "Pending")),
            image=image,
        )

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
            status=SandboxStatus(response.get("status", "Unknown")),
            image=response.get("image"),
            pool=response.get("pool"),
            created_at=response.get("created_at"),
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
    ) -> CodeResult:
        """Execute code in sandbox using Jupyter kernel.

        Args:
            name: Sandbox name.
            code: Code to execute.
            language: Programming language.
            timeout: Timeout in seconds.

        Returns:
            Code execution result.
        """
        request = ExecRequest(
            code=code,
            use_jupyter=True,
            timeout=timeout,
            language=language,
        )
        response = self._http.post(
            f"{self._sandbox_path(name)}/exec",
            json=request.model_dump(),
        )
        return CodeResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            success=response.get("success", False),
            execution_time_ms=response.get("execution_time_ms", 0),
            error_name=response.get("error_name"),
            error_value=response.get("error_value"),
            traceback=response.get("traceback"),
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
        response = self._http.post(
            f"{self._sandbox_path(name)}/exec",
            json=request.model_dump(),
        )
        return CommandResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            exit_code=response.get("exit_code", -1),
            duration_ms=response.get("duration_ms", 0),
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
            content_base64=base64.b64encode(content).decode("ascii"),
        )
        self._http.post(
            f"{self._sandbox_path(name)}/files",
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
        # URL-encode the path as query parameter
        return self._http.get_bytes(
            f"{self._sandbox_path(name)}/files/{path.lstrip('/')}"
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
            f"{self._sandbox_path(name)}/files",
            params={"path": path},
        )
        files = response.get("files", [])
        return [
            FileInfo(
                name=f["name"],
                path=f["path"],
                is_dir=f.get("is_dir", False),
                size=f.get("size", 0),
                modified=f.get("modified"),
            )
            for f in files
        ]
