"""HTTP client for sandbox API operations."""

from __future__ import annotations

import base64
import re
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
    EnvVarInput,
    ExecRequest,
    FileInfo,
    FileWriteRequest,
    SandboxInfo,
    SandboxInfoPage,
    SandboxStatus,
    parse_auto_idle_timeout,
    to_env_vars,
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


def _parse_sandbox_info(
    raw: dict,
    workspace: str,
    default_status: SandboxStatus = SandboxStatus.UNKNOWN,
) -> SandboxInfo:
    """Build a SandboxInfo from one raw backend Sandbox body.

    The backend has used both camelCase and snake_case spellings for these
    fields, and reports the phase as either ``status`` or ``phase``; accept
    every spelling so all endpoints stay in sync.
    """
    return SandboxInfo(
        name=raw["name"],
        workspace=workspace,
        status=_parse_status(raw.get("status") or raw.get("phase"), default_status),
        image=raw.get("image") or None,
        pool=raw.get("poolName") or raw.get("pool"),
        created_at=raw.get("createdAt") or raw.get("created_at"),
        auto_idle_timeout_seconds=parse_auto_idle_timeout(raw),
        last_error=raw.get("lastError") or raw.get("last_error"),
        preserves_process_state=raw.get(
            "preservesProcessState", raw.get("preserves_process_state", False)
        )
        is True,
    )


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

    raw_success_count = response.get("successCount", response.get("success_count"))
    raw_failure_count = response.get("failureCount", response.get("failure_count"))

    if raw_success_count is None:
        success_count = sum(1 for item in results if item.success)
    else:
        success_count = _require_response_count(raw_success_count, "successCount")
    if raw_failure_count is None:
        failure_count = len(results) - success_count
    else:
        failure_count = _require_response_count(raw_failure_count, "failureCount")

    raw_total = response.get("total")
    total = (
        len(results)
        if "total" not in response
        else _require_response_count(raw_total, "total")
    )

    success = response.get("success")
    if not isinstance(success, bool):
        raise ValueError("Batch file write response must include a boolean success")

    return BatchFileWriteResponse(
        success=success,
        total=total,
        success_count=success_count,
        failure_count=failure_count,
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


def _require_response_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Batch file write response {field} must be an integer")
    return value


def _is_timeout_execution_response(response: dict[str, object]) -> bool:
    error_names = (response.get("error_name"), response.get("errorName"))
    if any(
        isinstance(value, str) and re.search(r"timeout", value, re.I)
        for value in error_names
    ):
        return True

    structured_values = (
        response.get("error_value"),
        response.get("errorValue"),
        response.get("detail"),
    )
    if any(
        isinstance(value, str) and re.search(r"\btime(?:d)?\s*out\b", value, re.I)
        for value in structured_values
    ):
        return True

    stderr = response.get("stderr")
    return isinstance(stderr, str) and _is_timeout_stderr(stderr)


def _is_timeout_stderr(value: str) -> bool:
    return bool(
        re.search(r"^\s*(?:\[timeout:|timeout:)", value, re.I)
        or re.search(r"\b(?:execution|command|code)\s+timed\s+out\b", value, re.I)
    )


# Long-poll status GET (``?wait_phase=&timeout=``) tuning. The backend caps
# the hold at 30s and defaults to 20s; the SDK mirrors those numbers so it
# never asks for a window the backend will silently shorten. The margin is
# how much longer than the hold a request is allowed to take, covering the
# round trip of a response that only arrives when the hold expires.
_DEFAULT_WAIT_TIMEOUT_SECONDS = 20
_MAX_WAIT_TIMEOUT_SECONDS = 30
_WAIT_REQUEST_MARGIN_SECONDS = 5


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
        return f"/_platform/sandbox/{ws}/sandboxes"

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
        self, pool: str, auto_idle_timeout_seconds: int | None = None
    ) -> SandboxInfo:
        """Claim a sandbox from a warm pool.

        The backend accepts the claim with HTTP 202 and adopts a warm pod
        asynchronously, so the returned sandbox usually starts out Pending.
        Poll :meth:`get` (or ``Sandbox.wait_until_ready``) until it is Running.

        Args:
            pool: Name of the warm pool.
            auto_idle_timeout_seconds: Per-claim auto-idle override in seconds.

        Returns:
            Information about the claimed sandbox.
        """
        request = ClaimRequest(
            pool_name=pool,
            auto_idle_timeout_seconds=auto_idle_timeout_seconds,
        )
        response = self._http.post(
            f"{self._sandboxes_path()}/claim",
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        info = _parse_sandbox_info(
            response, self.config.workspace, SandboxStatus.PENDING
        )
        if info.pool is None:
            info.pool = pool
        if info.auto_idle_timeout_seconds is None:
            info.auto_idle_timeout_seconds = auto_idle_timeout_seconds
        return info

    def create(
        self,
        image: str,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        allow_internet_access: bool | None = None,
        auto_idle_timeout_seconds: int | None = None,
        env_vars: Sequence[EnvVarInput] | None = None,
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
            auto_idle_timeout_seconds: Per-sandbox auto-idle override in seconds.
            env_vars: Environment variables to inject into the sandbox. Each
                entry is an :class:`EnvVar` or an equivalent
                ``{"name": ..., "value": ...}`` mapping.
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
            auto_idle_timeout_seconds=auto_idle_timeout_seconds,
            env_vars=to_env_vars(env_vars),
            secret_refs=secret_refs,
        )
        # The backend has two create wire shapes: the internal route's
        # CreateSandboxRequest nests cpu/memory under `resources` (and
        # silently ignores top-level keys), while the external API-key
        # route's ExternalCreateRequest takes them flat (and ignores
        # `resources`). Send both; each route reads its own shape.
        payload = request.model_dump(by_alias=True, exclude_none=True)
        resources = {
            key: value for key, value in {"cpu": cpu, "memory": memory}.items() if value
        }
        if resources:
            payload["resources"] = resources
        response = self._http.post(self._sandboxes_path(), json=payload)
        info = _parse_sandbox_info(
            response, self.config.workspace, SandboxStatus.PENDING
        )
        info.image = info.image or image
        if info.auto_idle_timeout_seconds is None:
            info.auto_idle_timeout_seconds = auto_idle_timeout_seconds
        return info

    def list(self) -> list[SandboxInfo]:
        """List all sandboxes in the configured workspace.

        Returns:
            List of sandbox info objects.
        """
        response = self._http.get(
            self._sandboxes_path(),
        )
        sandboxes = response.get("sandboxes", [])
        return [_parse_sandbox_info(s, self.config.workspace) for s in sandboxes]

    def list_page(
        self,
        *,
        limit: int = 25,
        continue_token: str | None = None,
    ) -> SandboxInfoPage:
        """List one bounded page of sandboxes.

        There is exactly one name-ordered listing across every sandbox state.
        The continuation token is an opaque keyset cursor and must be reused
        with the same limit that produced it. An empty token means "no token":
        the backend rejects ``continueToken=`` with HTTP 422, so it is treated
        the same as ``None`` and requests the first page.
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        params: dict[str, str | int] = {"limit": limit}
        if continue_token:
            params["continueToken"] = continue_token
        response = self._http.get(self._sandboxes_path(), params=params)
        sandboxes = response.get("sandboxes", [])
        infos = [_parse_sandbox_info(s, self.config.workspace) for s in sandboxes]
        return SandboxInfoPage(
            sandboxes=infos,
            loaded=response.get("loaded", len(infos)),
            has_more=response.get("hasMore", False),
            continue_token=response.get("continueToken"),
        )

    def get(
        self,
        name: str,
        request_timeout: float | None = None,
        wait_phase: str | None = None,
        wait_timeout: float | None = None,
    ) -> SandboxInfo:
        """Get information about a sandbox.

        This is the poll target for every asynchronous lifecycle operation.

        Args:
            name: Sandbox name.
            request_timeout: Optional per-request timeout override, in
                seconds. Passing ``None`` (the default) leaves the client's
                configured timeout in place; callers polling toward a
                deadline (e.g. ``wait_until_ready``) should pass the
                remaining budget so a single stalled request cannot outlast
                the caller's overall timeout.
            wait_phase: Optional phase to long-poll for (e.g. ``"Running"``).
                When given, the backend holds the request open until the
                sandbox reaches that phase or its own wait window elapses,
                and answers with the current payload either way — a response
                that still reports another phase is a normal timeout, not an
                error. Backends that predate the parameter ignore it and
                answer immediately, which degrades to plain polling.
            wait_timeout: How long, in seconds, the backend may hold the
                request when ``wait_phase`` is set. Defaults to
                ``_DEFAULT_WAIT_TIMEOUT_SECONDS`` and is clamped to the
                server-side cap.

        Returns:
            Information about the sandbox.
        """
        kwargs: dict[str, object] = {}
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        if wait_phase is not None:
            hold = int(
                min(
                    _MAX_WAIT_TIMEOUT_SECONDS,
                    _DEFAULT_WAIT_TIMEOUT_SECONDS
                    if wait_timeout is None
                    else wait_timeout,
                )
            )
            if request_timeout is not None:
                # The server holds the connection for the whole wait window
                # before answering, so the per-request timeout must outlast
                # it — same reasoning as the request_timeout cap above, in
                # the other direction. Leave a margin for the response trip
                # so a long-poll that answers right at its cap still lands.
                # Once too little budget is left to hold anything, fall back
                # to a plain (immediate) status GET.
                hold = min(hold, int(request_timeout) - _WAIT_REQUEST_MARGIN_SECONDS)
            if hold >= 1:
                kwargs["params"] = {"wait_phase": wait_phase, "timeout": hold}
        response = self._http.get(self._sandbox_path(name), **kwargs)
        return _parse_sandbox_info(response, self.config.workspace)

    def ping_kernel(
        self,
        name: str,
        wait_timeout: int,
        request_timeout: float | None = None,
    ) -> None:
        """Block on the sandbox agent until its Jupyter kernel is warm.

        Calls ``GET <sandbox>/ping?wait=kernel&timeout=<wait_timeout>`` on
        the sandbox agent (the same per-sandbox base path ``exec`` uses, so
        it is proxied exactly like code execution). Returning normally means
        the agent answered 200 — the kernel has started.

        Args:
            name: Sandbox name.
            wait_timeout: Seconds the agent may block waiting for the kernel.
            request_timeout: Optional per-request timeout override.

        Raises:
            NotFoundError: The agent (or the proxy in front of it) does not
                expose the endpoint — an older agent, so the caller must fall
                back to probing the kernel through ``exec``.
            ProKubeError: Any other non-2xx answer. ``status_code == 503``
                means the kernel is not warm yet and the call may be retried;
                400/405 likewise indicate an agent without this endpoint.
        """
        kwargs: dict[str, object] = {
            "params": {"wait": "kernel", "timeout": wait_timeout},
        }
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        # The agent answers plain text ("pong"), not JSON: fetch bytes so the
        # shared error translation still runs without a JSON parse of the body.
        self._http.get_bytes(self._sandbox_sub_path(name, "ping"), **kwargs)

    def pause(self, name: str) -> SandboxInfo:
        """Pause a running sandbox.

        Frees compute resources while preserving /workspace and /home/agent.
        The backend accepts the request with HTTP 202 and reports phase
        ``Pausing`` until its worker settles the sandbox on ``Paused``; poll
        :meth:`get` to observe the final phase.

        Args:
            name: Sandbox name.

        Returns:
            Sandbox information as of the accepted pause request.

        Raises:
            SandboxError: If sandbox is not in Running state (HTTP 409).
        """
        try:
            response = self._http.post(self._sandbox_sub_path(name, "pause"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise
        return _parse_sandbox_info(
            response, self.config.workspace, SandboxStatus.PAUSING
        )

    def resume(self, name: str) -> SandboxInfo:
        """Resume a paused sandbox.

        A new pod starts with the same PVC mounts at /workspace and /home/agent.
        The backend accepts the request with HTTP 202 and reports phase
        ``Resuming`` until the new pod is up; poll :meth:`get` until Running.

        Args:
            name: Sandbox name.

        Returns:
            Sandbox information as of the accepted resume request.

        Raises:
            SandboxError: If sandbox is not in Paused state (HTTP 409).
        """
        try:
            response = self._http.post(self._sandbox_sub_path(name, "resume"))
        except ProKubeError as e:
            if e.status_code == 409:
                raise SandboxError(str(e), status_code=409) from e
            raise
        return _parse_sandbox_info(
            response, self.config.workspace, SandboxStatus.RESUMING
        )

    def delete(self, name: str) -> None:
        """Delete a sandbox.

        The backend accepts the request with HTTP 202 and an empty body, then
        tears the sandbox down (including its persistence records)
        asynchronously. Poll :meth:`get` until it raises
        :class:`NotFoundError` to know the name has been released.

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
        timed_out = _is_timeout_execution_response(response)
        return CodeResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            success=response.get("success", False) and not timed_out,
            execution_time_ms=response.get(
                "durationMs", response.get("execution_time_ms", 0)
            ),
            error_name=response.get("errorName") or response.get("error_name"),
            error_value=response.get("errorValue") or response.get("error_value"),
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
        timed_out = _is_timeout_execution_response(response)
        exit_code = response.get("exitCode", response.get("exit_code", -1))
        return CommandResult(
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
            exit_code=-1 if timed_out else exit_code,
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
