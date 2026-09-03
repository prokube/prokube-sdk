"""Minimal client for the incompatible pk-sandbox v0.1 API."""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_SandboxRuntime = Literal["python", "python-microvm"]


class SandboxAPIError(Exception):
    """Base error returned by the v0.1 Sandbox API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None,
        operation: str,
        sandbox_name: str | None = None,
        retryable: bool = False,
        retry_after: str | None = None,
        cleanup_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.operation = operation
        self.sandbox_name = sandbox_name
        self.retryable = retryable
        self.retry_after = retry_after
        self.cleanup_required = cleanup_required


class SandboxAuthorizationError(SandboxAPIError):
    """The caller is not authenticated or authorized for the operation."""


class SandboxNotFoundError(SandboxAPIError):
    """The requested runtime or Sandbox does not exist."""


class SandboxConflictError(SandboxAPIError):
    """The requested Sandbox identity conflicts with existing intent."""


class SandboxCapacityError(SandboxAPIError):
    """The Sandbox cannot currently be scheduled or reached."""


class SandboxOperationTimeoutError(SandboxAPIError):
    """The operation exceeded its server-side deadline."""


class SandboxTransportError(SandboxAPIError):
    """The deployment could not be reached or returned an invalid transport response."""


@dataclass(frozen=True)
class CodeResult:
    """Result of one stateful Python execution."""

    stdout: str
    stderr: str
    success: bool
    exit_code: int
    duration_ms: int
    session_id: str
    outputs: list[Any] = field(default_factory=list)
    error_name: str = ""
    error_value: str = ""
    traceback: list[str] | None = None


@dataclass(frozen=True)
class FileInfo:
    """One direct child returned by a Sandbox directory listing."""

    name: str
    path: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class BatchFileResult:
    """Result of one item in a batch file upload."""

    index: int
    path: str
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class BatchFileWriteResult:
    """Aggregate result of a best-effort batch file upload."""

    success: bool
    total: int
    success_count: int
    failure_count: int
    results: list[BatchFileResult]


class FileManager:
    """Upload, download, and list files in one Sandbox."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def write(self, path: str, content: bytes | str) -> None:
        """Write bytes or UTF-8 text to a Sandbox path."""
        self._sandbox._check_available()
        if not path:
            raise ValueError("path must not be empty")
        payload = self._encode(path, content)
        self._sandbox._client._request(
            "POST",
            f"/{quote(self._sandbox.name, safe='')}/files",
            operation="write_file",
            sandbox_name=self._sandbox.name,
            json=payload,
        )

    def write_batch(
        self, items: Sequence[tuple[str, bytes | str]]
    ) -> BatchFileWriteResult:
        """Write up to 100 files in request order and return per-file results."""
        self._sandbox._check_available()
        if not 1 <= len(items) <= 100:
            raise ValueError("batch must contain 1 to 100 items")
        payload = [self._encode(path, content) for path, content in items]
        raw = self._sandbox._client._request(
            "POST",
            f"/{quote(self._sandbox.name, safe='')}/files/batch",
            operation="write_files",
            sandbox_name=self._sandbox.name,
            json={"items": payload},
        )
        results = [
            BatchFileResult(
                index=int(item.get("index", 0)),
                path=str(item.get("path", "")),
                success=bool(item.get("success", False)),
                error=str(item["error"]) if item.get("error") else None,
            )
            for item in raw.get("results", [])
            if isinstance(item, dict)
        ]
        return BatchFileWriteResult(
            success=bool(raw.get("success", False)),
            total=int(raw.get("total", len(items))),
            success_count=int(raw.get("successCount", 0)),
            failure_count=int(raw.get("failureCount", 0)),
            results=results,
        )

    def read(self, path: str) -> bytes:
        """Read a Sandbox file as bytes."""
        self._sandbox._check_available()
        if not path:
            raise ValueError("path must not be empty")
        return self._sandbox._client._request_bytes(
            "GET",
            f"/{quote(self._sandbox.name, safe='')}/files/download",
            operation="read_file",
            sandbox_name=self._sandbox.name,
            params={"path": path},
        )

    def list(self, path: str = "/workspace") -> list[FileInfo]:
        """List direct children of a Sandbox directory."""
        self._sandbox._check_available()
        if not path:
            raise ValueError("path must not be empty")
        raw = self._sandbox._client._request(
            "GET",
            f"/{quote(self._sandbox.name, safe='')}/files",
            operation="list_files",
            sandbox_name=self._sandbox.name,
            params={"path": path},
        )
        return [
            FileInfo(
                name=str(item.get("name", "")),
                path=str(item.get("path", "")),
                is_dir=bool(item.get("isDirectory", False)),
                size=int(item.get("size", 0)),
            )
            for item in raw.get("files", [])
            if isinstance(item, dict)
        ]

    @staticmethod
    def _encode(path: str, content: bytes | str) -> dict[str, str]:
        if not path:
            raise ValueError("path must not be empty")
        if isinstance(content, str):
            return {"path": path, "content": content}
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes or str")
        return {
            "path": path,
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }


class Sandbox:
    """A stable Sandbox identity backed by an automatically managed Actor."""

    def __init__(
        self,
        client: SandboxClient,
        *,
        name: str,
        runtime: _SandboxRuntime,
        size: Literal["small"],
        network: Literal["offline"],
        metadata: dict[str, Any],
    ) -> None:
        self._client = client
        self.name = name
        self.runtime: _SandboxRuntime = runtime
        self.size: Literal["small"] = size
        self.network: Literal["offline"] = network
        self.metadata = metadata
        self._deleted = False
        self.files = FileManager(self)

    def run_code(self, code: str, *, timeout: int = 300) -> CodeResult:
        """Run Python in the Sandbox's persistent execution context."""
        self._check_available()
        if not code:
            raise ValueError("code must not be empty")
        if not 1 <= timeout <= 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        raw = self._client._request(
            "POST",
            f"/{quote(self.name, safe='')}/exec",
            operation="run_code",
            sandbox_name=self.name,
            json={"code": code, "timeout": timeout},
            timeout=timeout + 10,
        )
        return CodeResult(
            stdout=str(raw.get("stdout", "")),
            stderr=str(raw.get("stderr", "")),
            success=bool(raw.get("success", False)),
            exit_code=int(raw.get("exitCode", 0)),
            duration_ms=int(raw.get("durationMs", 0)),
            session_id=str(raw.get("session_id", "")),
            outputs=list(raw.get("outputs") or []),
            error_name=str(raw.get("error_name", "")),
            error_value=str(raw.get("error_value", "")),
            traceback=raw.get("traceback"),
        )

    def delete(self) -> None:
        """Idempotently delete this Sandbox."""
        if self._deleted:
            return
        try:
            self._client._request(
                "DELETE",
                f"/{quote(self.name, safe='')}",
                operation="delete",
                sandbox_name=self.name,
            )
        except SandboxNotFoundError:
            pass
        self._deleted = True

    def _check_available(self) -> None:
        if self._deleted:
            raise RuntimeError(f"Sandbox {self.name!r} has been deleted")

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.delete()


class SandboxClient:
    """Client for the deliberately small pk-sandbox v0.1 contract."""

    def __init__(
        self,
        *,
        endpoint: str,
        workspace: str,
        api_key: str | None = None,
        user_id: str | None = None,
        timeout: float = 300,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        if not workspace:
            raise ValueError("workspace is required")
        if api_key and user_id:
            raise ValueError("api_key and user_id are mutually exclusive")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint must be an absolute HTTP(S) URL")

        self.workspace = workspace
        self._api_key = api_key
        if api_key:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            headers = {"x-api-key": api_key}
            route = f"/sandbox/{quote(workspace, safe='')}/sandboxes"
        else:
            base_url = endpoint.rstrip("/")
            headers = {"kubeflow-userid": user_id} if user_id else {}
            route = f"/api/namespaces/{quote(workspace, safe='')}/sandboxes"
        self._route = route
        self._http = httpx.Client(
            base_url=base_url + "/", headers=headers, timeout=timeout
        )

    def create(
        self,
        *,
        name: str | None = None,
        runtime: _SandboxRuntime = "python",
        size: Literal["small"] = "small",
        network: Literal["offline"] = "offline",
    ) -> Sandbox:
        """Idempotently create or reconnect to a Sandbox."""
        name = name or f"sandbox-{uuid.uuid4().hex[:12]}"
        if len(name) > 63 or not _NAME_PATTERN.fullmatch(name):
            raise ValueError("name must be a valid DNS label of at most 63 characters")
        if runtime not in {"python", "python-microvm"}:
            raise ValueError("runtime must be python or python-microvm")
        if size != "small":
            raise ValueError("size must be small")
        if network != "offline":
            raise ValueError("network must be offline")

        raw = self._request(
            "POST",
            "",
            operation="create",
            sandbox_name=name,
            json={
                "name": name,
                "runtime": runtime,
                "size": size,
                "network": network,
            },
        )
        return Sandbox(
            self,
            name=name,
            runtime=runtime,
            size=size,
            network=network,
            metadata=raw,
        )

    def close(self) -> None:
        """Close pooled HTTP connections without deleting Sandboxes."""
        self._http.close()

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        suffix: str,
        *,
        operation: str,
        sandbox_name: str | None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self._send(
            method,
            suffix,
            operation=operation,
            sandbox_name=sandbox_name,
            timeout=timeout,
            **kwargs,
        )
        return response.json() if response.content else {}

    def _request_bytes(
        self,
        method: str,
        suffix: str,
        *,
        operation: str,
        sandbox_name: str,
        **kwargs: Any,
    ) -> bytes:
        return self._send(
            method,
            suffix,
            operation=operation,
            sandbox_name=sandbox_name,
            **kwargs,
        ).content

    def _send(
        self,
        method: str,
        suffix: str,
        *,
        operation: str,
        sandbox_name: str | None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = self._http.request(
                method,
                (self._route + suffix).lstrip("/"),
                timeout=timeout,
                **kwargs,
            )
        except httpx.TimeoutException as error:
            raise SandboxOperationTimeoutError(
                f"{operation} timed out",
                status_code=0,
                request_id=None,
                operation=operation,
                sandbox_name=sandbox_name,
                retryable=True,
            ) from error
        except httpx.RequestError as error:
            raise SandboxTransportError(
                f"{operation} transport failed: {error}",
                status_code=0,
                request_id=None,
                operation=operation,
                sandbox_name=sandbox_name,
                retryable=True,
            ) from error
        if response.is_success:
            return response

        body: dict[str, Any]
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body = {}
        detail = str(body.get("detail") or response.text or response.reason_phrase)
        attributes = {
            "status_code": response.status_code,
            "request_id": response.headers.get("x-request-id"),
            "operation": operation,
            "sandbox_name": sandbox_name,
            "retryable": bool(
                body.get("retryable", response.status_code in {429, 503})
            ),
            "retry_after": response.headers.get("retry-after"),
            "cleanup_required": bool(body.get("cleanupRequired", False)),
        }
        error_type: type[SandboxAPIError]
        if response.status_code in {401, 403}:
            error_type = SandboxAuthorizationError
        elif response.status_code == 404:
            error_type = SandboxNotFoundError
        elif response.status_code == 409:
            error_type = SandboxConflictError
        elif response.status_code in {429, 502, 503}:
            error_type = SandboxCapacityError
        elif response.status_code == 504:
            error_type = SandboxOperationTimeoutError
        else:
            error_type = SandboxAPIError
        raise error_type(
            f"{operation} failed ({response.status_code}): {detail}", **attributes
        )


__all__ = [
    "BatchFileResult",
    "BatchFileWriteResult",
    "CodeResult",
    "FileInfo",
    "FileManager",
    "Sandbox",
    "SandboxAPIError",
    "SandboxAuthorizationError",
    "SandboxCapacityError",
    "SandboxClient",
    "SandboxConflictError",
    "SandboxNotFoundError",
    "SandboxOperationTimeoutError",
    "SandboxTransportError",
]
