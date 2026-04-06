"""Pydantic models for sandbox operations."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SandboxStatus(str, Enum):
    """Status of a sandbox."""

    PENDING = "Pending"
    RUNNING = "Running"
    PAUSED = "Paused"
    BOUND = "Bound"  # Claim is bound to a sandbox (ready to use)
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class SandboxInfo(BaseModel):
    """Information about a sandbox."""

    name: str = Field(..., description="Name of the sandbox")
    workspace: str = Field(..., description="Workspace (Kubernetes namespace)")
    status: SandboxStatus = Field(
        default=SandboxStatus.UNKNOWN, description="Current status"
    )
    image: str | None = Field(default=None, description="Container image")
    pool: str | None = Field(default=None, description="WarmPool name if claimed")
    created_at: str | None = Field(default=None, description="Creation timestamp")


class CommandResult(BaseModel):
    """Result of a shell command execution."""

    stdout: str = Field(default="", description="Standard output")
    stderr: str = Field(default="", description="Standard error")
    exit_code: int = Field(..., description="Exit code (0 = success)")
    duration_ms: int = Field(default=0, description="Execution time in milliseconds")

    @property
    def success(self) -> bool:
        """Check if command succeeded."""
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined stdout and stderr for convenience."""
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr


class CodeResult(BaseModel):
    """Result of code execution in Jupyter kernel."""

    stdout: str = Field(default="", description="Standard output")
    stderr: str = Field(default="", description="Standard error")
    success: bool = Field(..., description="Whether execution succeeded")
    execution_time_ms: int = Field(
        default=0, description="Execution time in milliseconds"
    )
    error_name: str | None = Field(default=None, description="Error type if failed")
    error_value: str | None = Field(default=None, description="Error message if failed")
    traceback: list[str] | None = Field(default=None, description="Traceback if failed")
    session_id: str | None = Field(
        default=None, description="Session ID for stateful execution"
    )

    @property
    def output(self) -> str:
        """Combined stdout and stderr for convenience."""
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr


class FileInfo(BaseModel):
    """Information about a file in the sandbox."""

    name: str = Field(..., description="File name")
    path: str = Field(..., description="Full path")
    is_dir: bool = Field(default=False, description="Whether this is a directory")
    size: int = Field(default=0, description="File size in bytes")
    modified: str | None = Field(default=None, description="Last modified timestamp")


class ExecRequest(BaseModel):
    """Request to execute code or command in sandbox."""

    code: str = Field(..., description="Code or command to execute")
    use_jupyter: bool = Field(
        default=True, description="Use Jupyter kernel (stateful) vs shell"
    )
    timeout: int = Field(default=300, description="Timeout in seconds")
    language: str = Field(default="python", description="Language for Jupyter kernel")
    session_id: str | None = Field(
        default=None, description="Session ID for stateful Jupyter execution"
    )
    reset_session: bool = Field(
        default=False, description="Restart kernel before executing code"
    )


class ClaimRequest(BaseModel):
    """Request to claim a sandbox from a warm pool."""

    pool_name: str = Field(
        ..., serialization_alias="poolName", description="Name of the warm pool"
    )


class CreateRequest(BaseModel):
    """Request to create a new sandbox."""

    image: str = Field(..., description="Container image to use")
    name: str | None = Field(default=None, description="Optional sandbox name")



class FileWriteRequest(BaseModel):
    """Request to write a file to sandbox."""

    path: str = Field(..., description="Path where to write the file")
    content: str = Field(..., description="Base64-encoded file content")
