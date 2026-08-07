"""Main Sandbox class for interacting with prokube sandboxes."""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

import httpx

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import (
    NotFoundError,
    ProKubeError,
    SandboxError,
    SandboxTimeoutError,
)
from prokube.sandbox.client import SandboxClient
from prokube.sandbox.code import CodeRunner
from prokube.sandbox.commands import CommandRunner
from prokube.sandbox.files import FileManager
from prokube.sandbox.models import (
    CodeResult,
    EnvVarInput,
    SandboxInfo,
    SandboxStatus,
)

logger = logging.getLogger(__name__)


SandboxT = TypeVar("SandboxT", bound="Sandbox")


@dataclass(frozen=True)
class SandboxPage(Generic[SandboxT]):
    """One bounded page of ready-to-use sandboxes.

    Every sandbox on the page owns its own HTTP client. Call :meth:`close`
    (or use the page as a context manager) to release those clients without
    destroying the remote sandboxes; ``kill()`` is the only other path that
    closes them, and it deletes the sandbox too.
    """

    sandboxes: list[SandboxT]
    loaded: int
    has_more: bool
    continue_token: str | None = None

    def close(self) -> None:
        """Release the HTTP client of every sandbox on this page.

        Closing is idempotent and leaves the remote sandboxes untouched.
        """
        for sandbox in self.sandboxes:
            sandbox._client.close()

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager - releases the page's HTTP clients."""
        self.close()


class Sandbox:
    """A sandbox environment for executing code and commands.

    Sandboxes provide isolated environments for running code safely.
    They can be created directly or claimed from a warm pool for faster startup.

    Example:
        >>> # Claim from warm pool (fast, but adoption is asynchronous)
        >>> sbx = Sandbox.from_pool("python-pool")
        >>> sbx.wait_until_ready()
        >>>
        >>> # Execute code (stateful)
        >>> sbx.run_code("import pandas as pd")
        >>> sbx.run_code("df = pd.read_csv('/workspace/data.csv')")
        >>> result = sbx.run_code("print(df.head())")
        >>> print(result.stdout)
        >>>
        >>> # Run shell commands
        >>> result = sbx.commands.run("pip install numpy")
        >>>
        >>> # File operations
        >>> sbx.files.write("/workspace/test.txt", "hello world")
        >>> content = sbx.files.read("/workspace/test.txt")
        >>>
        >>> # Cleanup (deletion completes asynchronously)
        >>> sbx.kill(wait=True)

    Context Manager:
        >>> with Sandbox.from_pool("python-pool") as sbx:
        ...     sbx.wait_until_ready()
        ...     result = sbx.run_code("print(42)")
        ...     print(result.stdout)
        # Sandbox is automatically killed
    """

    def __init__(
        self,
        name: str,
        workspace: str,
        client: SandboxClient,
        status: SandboxStatus = SandboxStatus.RUNNING,
        pool: str | None = None,
        image: str | None = None,
        auto_idle_timeout_seconds: int | None = None,
    ) -> None:
        """Initialize a Sandbox instance.

        Note: Use Sandbox.from_pool() or Sandbox.create() instead of
        calling this constructor directly.

        Args:
            name: Sandbox name.
            workspace: Workspace (Kubernetes namespace).
            client: Sandbox API client.
            status: Current sandbox status.
            pool: WarmPool name if claimed from pool.
            image: Container image if created directly.
            auto_idle_timeout_seconds: Auto-idle timeout override in seconds.
        """
        self._name = name
        self._workspace = workspace
        self._client = client
        self._status = status
        self._pool = pool
        self._image = image
        self._auto_idle_timeout_seconds = auto_idle_timeout_seconds
        self._killed = False
        self._delete_requested = False
        self._last_error: str | None = None

        # Initialize helpers with killed-state check callback
        self._commands = CommandRunner(client, name, self._check_not_killed)
        self._files = FileManager(client, name, self._check_not_killed)
        self._code = CodeRunner(client, name, self._check_not_killed)

    def _check_not_killed(self) -> None:
        """Raise error if the sandbox has been killed or is being deleted."""
        if self._killed:
            raise SandboxError(
                f"Sandbox {self._name} has been killed and cannot be used anymore"
            )
        if self._delete_requested:
            raise SandboxError(
                f"Sandbox {self._name} is being deleted; call kill(wait=True) "
                "to wait for the deletion to complete"
            )

    @property
    def name(self) -> str:
        """Get the sandbox name."""
        return self._name

    @property
    def workspace(self) -> str:
        """Get the workspace (Kubernetes namespace)."""
        return self._workspace

    @property
    def status(self) -> str:
        """Get the current status."""
        return self._status.value

    @property
    def phase(self) -> str:
        """Current sandbox phase (Running, Paused, Pending, etc.).

        Refreshes from the API to return the latest phase.
        """
        self.refresh()
        return self._status.value

    @property
    def commands(self) -> CommandRunner:
        """Get the command runner for shell commands."""
        self._check_not_killed()
        return self._commands

    @property
    def files(self) -> FileManager:
        """Get the file manager for file operations."""
        self._check_not_killed()
        return self._files

    def run_code(
        self,
        code: str,
        language: str = "python",
        timeout: int = 300,
    ) -> CodeResult:
        """Execute code in the Jupyter kernel.

        The kernel maintains state between calls - variables and imports
        persist, similar to running cells in a Jupyter notebook.

        Args:
            code: Code to execute.
            language: Programming language (default: python).
            timeout: Timeout in seconds (default: 300).

        Returns:
            CodeResult with stdout, stderr, success, and execution time.

        Example:
            >>> sbx.run_code("x = 42")
            >>> result = sbx.run_code("print(x * 2)")
            >>> print(result.stdout)  # "84"
        """
        self._check_not_killed()
        return self._code.run(code, language=language, timeout=timeout)

    def reset_session(self) -> None:
        """Reset the Jupyter kernel session.

        The next run_code() call will restart the kernel and clear all
        variables and imports from previous executions.

        Example:
            >>> sbx.run_code("x = 42")
            >>> sbx.reset_session()
            >>> result = sbx.run_code("print('x' in dir())")  # False
        """
        self._check_not_killed()
        self._code.reset_session()

    @property
    def session_id(self) -> str | None:
        """Get the current Jupyter session ID, if any.

        Returns None if no code has been executed yet.
        """
        return self._code.session_id

    @property
    def auto_idle_timeout_seconds(self) -> int | None:
        """Get the configured auto-idle timeout override in seconds, if known."""
        return self._auto_idle_timeout_seconds

    def pause(self, wait: bool = True, timeout: int = 300) -> None:
        """Pause the sandbox. Frees compute resources.

        Preserves: /workspace (working directory) and /home/agent (HOME, pip --user, dotfiles).
        Lost: running processes, apt-installed system packages, /tmp.

        The backend accepts the pause asynchronously (phase ``Pausing``). By
        default this method blocks until the sandbox reports ``Paused``.

        Args:
            wait: Block until the sandbox has actually reached Paused
                (default: True). Pass False to return as soon as the backend
                accepted the request, leaving the phase at ``Pausing``.
            timeout: Maximum seconds to wait when ``wait`` is True.

        Raises:
            SandboxError: If sandbox is not in Running state, or if the pause
                fails (phase ``Failed``). Re-issuing ``pause()`` retries.
            SandboxTimeoutError: If the sandbox does not reach Paused within
                ``timeout`` seconds.
        """
        self._check_not_killed()
        info = self._client.pause(self._name)
        self._status = info.status
        self._last_error = info.last_error
        # Pausing deletes the underlying pod, so any existing Jupyter session
        # is no longer valid. Reset so next run_code() starts a fresh kernel.
        self._code.reset_session()
        if wait:
            self._wait_for_pause(timeout)

    def _wait_for_pause(self, timeout: int) -> None:
        """Poll until the sandbox settles on Paused, Failed, or the deadline."""
        poll_interval = 2
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.refresh(request_timeout=remaining)
            except httpx.TimeoutException:
                pass
            except NotFoundError:
                # A concurrently admitted delete finished while we waited.
                raise SandboxError(
                    f"Sandbox {self._name} was deleted while waiting for it to pause"
                ) from None
            else:
                if self._status == SandboxStatus.PAUSED:
                    return
                if self._status == SandboxStatus.FAILED:
                    raise SandboxError(
                        f"Sandbox {self._name} failed to pause: "
                        f"{self._last_error or 'no error reported by the backend'} "
                        f"(re-issue pause() to retry)"
                    )
                if self._status == SandboxStatus.DELETING:
                    # Delete outranks pause on the backend: the pause worker's
                    # settle will miss and the sandbox is going away. Waiting
                    # any longer can only end in 404.
                    raise SandboxError(
                        f"Sandbox {self._name} is being deleted; it will "
                        "never reach Paused"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        raise SandboxTimeoutError(
            f"Sandbox {self._name} did not pause within {timeout}s "
            f"(current phase: {self._status.value!r})"
        )

    def resume(self) -> None:
        """Resume a paused sandbox.

        A new pod starts with the same PVC mounts at /workspace and /home/agent.
        If /home/agent/.sandbox-restore.sh exists, it runs automatically on
        startup to reinstall system packages.

        The backend accepts the resume asynchronously and reports phase
        ``Resuming``; this call does not block. Use
        :meth:`wait_until_ready` to wait for the new pod to become Running.

        Raises:
            SandboxError: If sandbox is not in Paused state.
        """
        self._check_not_killed()
        info = self._client.resume(self._name)
        self._status = info.status
        self._last_error = info.last_error
        if info.auto_idle_timeout_seconds is not None:
            self._auto_idle_timeout_seconds = info.auto_idle_timeout_seconds
        # New pod means previous Jupyter session is invalid.
        self._code.reset_session()

    def wait_until_ready(self, timeout: int = 120) -> None:
        """Block until sandbox phase is Running. Useful after resume().

        Transitional phases (Pending, Pausing, Resuming) simply keep polling.

        Args:
            timeout: Maximum seconds to wait (default: 120).

        Raises:
            SandboxTimeoutError: If sandbox does not become Running within timeout.
            SandboxError: If the sandbox enters a state from which it can no
                longer become ready (Failed, Succeeded, or Deleting). For a
                failed sandbox the backend's ``lastError`` is included.
        """
        self._check_not_killed()
        poll_interval = 2
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # Cap the GET at the remaining budget so a single stalled
                # poll cannot block past the caller's deadline: without this,
                # the request falls back to the client's default timeout
                # (PROKUBE_TIMEOUT, 300s), which can vastly exceed a short
                # wait_until_ready(timeout=...) call.
                self.refresh(request_timeout=remaining)
            except httpx.TimeoutException:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))
                continue
            if self._status == SandboxStatus.RUNNING:
                self._warmup_kernel(deadline)
                return
            if self._status in (
                SandboxStatus.FAILED,
                SandboxStatus.SUCCEEDED,
                SandboxStatus.DELETING,
            ):
                detail = f": {self._last_error}" if self._last_error else ""
                raise SandboxError(
                    f"Sandbox {self._name} entered terminal state "
                    f"{self._status.value!r} while waiting for it to become "
                    f"ready{detail}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        raise SandboxTimeoutError(
            f"Sandbox {self._name} did not become ready within {timeout}s "
            f"(current phase: {self._status.value!r})"
        )

    def _warmup_kernel(self, deadline: float) -> None:
        """Probe the sandbox interpreter until it echoes a unique marker back.

        The first execution after a pod reaches Running can race a cold
        interpreter: a probe may return successfully with empty stdout before
        the execution pipeline is fully live, in which case the first user
        ``run_code()`` call would silently return empty output. (Whether the
        current sandbox agent still exhibits this race is unverified; the
        probe is kept as cheap insurance until warmup is re-measured against
        it.)

        This method hides that race by running a tiny ``print(<marker>)``
        probe in a loop until the marker appears in stdout, proving the
        execution pipeline is end-to-end live. The check is containment, not
        equality: the interpreter may append unrelated text (e.g. warnings)
        to the same stream, and extra output does not make the session any
        less live. The probe is bounded by ``deadline`` (the same deadline
        used by :meth:`wait_until_ready`), so it can never exceed the
        caller's overall timeout budget. If the deadline is reached without
        success, a warning is logged and the method returns without raising —
        the user may still get useful results, and we don't want to block
        ``create`` when the workaround is only partially working.

        Notes:
            * The marker is per-call (``uuid4().hex``) to avoid collisions
              with any user code that happens to print a similar literal.
            * If a probe returns successfully but stdout does not contain the
              marker, discard that session before retrying. Otherwise a
              cold/stale session can be reused forever and every probe keeps
              returning empty stdout.

        Args:
            deadline: ``time.monotonic()`` value after which the probe gives
                up and returns without raising.
        """
        self._check_not_killed()
        marker = f"__pk_warmup_{uuid.uuid4().hex}__"
        code = f'print("{marker}")'
        # Cap the per-probe backend timeout so a single warmup attempt cannot
        # consume the entire wait_until_ready budget. Without this cap, the
        # first probe call against a stuck kernel could block the SDK for the
        # user's full timeout (potentially minutes) and starve the intended
        # 0.5s retry loop.
        max_probe_timeout = 5
        attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            # run_code expects an integer second timeout, so we cannot probe
            # with a sub-second budget. Once less than 1s remains the only
            # way to stay strictly within wait_until_ready's deadline is to
            # give up rather than clamp upward and overrun.
            if remaining < 1:
                logger.warning(
                    "Sandbox %s kernel warmup probe did not echo marker "
                    "within deadline after %d attempt(s); continuing anyway",
                    self._name,
                    attempts,
                )
                return
            attempts += 1
            # Cap each probe so retries stay frequent against a stuck kernel,
            # while still never exceeding the remaining wait_until_ready budget.
            probe_timeout = min(max_probe_timeout, int(remaining))
            try:
                result = self.run_code(code, timeout=probe_timeout)
            except ProKubeError as exc:
                if exc.status_code != 504:
                    raise
                self._code.reset_session()
                sleep_for = min(0.5, max(0.0, deadline - time.monotonic()))
                time.sleep(sleep_for)
                continue
            if marker in result.stdout:
                return
            self._code.reset_session()
            # Loop top will recompute remaining and exit if deadline passed.
            sleep_for = min(0.5, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)

    def kill(self, wait: bool = False, timeout: int = 300) -> None:
        """Destroy the sandbox.

        The backend accepts the delete with HTTP 202 and tears the sandbox
        down asynchronously, including purging its persistence records. The
        sandbox name stays reserved until that purge completes, so pass
        ``wait=True`` when you intend to reuse the name (or need the quota
        back) and must know the reclamation finished.

        After a successful ``kill()`` (or once the delete has been admitted),
        the sandbox cannot be used anymore: run_code(), commands, and files
        raise. If waiting fails or times out, the object stays in a
        deletion-requested state — normal operations are blocked, but
        ``kill(wait=True)`` may be re-issued to keep waiting (the backend's
        DELETE is idempotent while teardown is in flight).

        If the initial delete request itself fails, an exception is raised
        and the sandbox remains usable so callers can retry.

        Args:
            wait: Poll until the sandbox is really gone (default: False).
            timeout: Maximum seconds to wait when ``wait`` is True.

        Raises:
            SandboxError: If ``wait`` is True and the backend lands the
                delete in a terminal failure (phase ``Failed`` with
                ``lastError``); re-issue ``kill()`` to retry the delete.
            SandboxTimeoutError: If ``wait`` is True and the sandbox is still
                present after ``timeout`` seconds.
        """
        if self._killed:
            return  # Already killed, nothing to do
        try:
            self._client.delete(self._name)
        except NotFoundError:
            # A re-issued kill can find the sandbox already gone: that is
            # the outcome we wanted, not an error.
            self._status = SandboxStatus.SUCCEEDED
            self._killed = True
            self._client.close()
            return
        # The delete is admitted: from here the sandbox is going away and
        # must not accept work, even if the wait below fails or times out.
        self._delete_requested = True
        if wait:
            self._wait_until_gone(timeout)
        # Only mark as killed and close client after successful delete
        self._status = SandboxStatus.SUCCEEDED
        self._killed = True
        self._client.close()

    def _wait_until_gone(self, timeout: int) -> None:
        """Poll the sandbox until the backend reports it as absent (404)."""
        poll_interval = 2
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                info = self._client.get(self._name, request_timeout=remaining)
            except NotFoundError:
                return
            except httpx.TimeoutException:
                pass
            else:
                self._status = info.status
                self._last_error = info.last_error
                if self._status == SandboxStatus.FAILED:
                    # delete_failed on the backend: retries are exhausted and
                    # the row (and name) stay reserved until a delete is
                    # re-issued and succeeds.
                    raise SandboxError(
                        f"Sandbox {self._name} failed to delete: "
                        f"{self._last_error or 'no error reported by the backend'} "
                        f"(re-issue kill() to retry)"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        raise SandboxTimeoutError(
            f"Sandbox {self._name} was not deleted within {timeout}s "
            f"(current phase: {self._status.value!r})"
        )

    def refresh(self, request_timeout: float | None = None) -> None:
        """Refresh sandbox information from the API.

        Args:
            request_timeout: Optional per-request timeout override, in
                seconds. See :meth:`SandboxClient.get`.
        """
        self._check_not_killed()
        info = self._client.get(self._name, request_timeout=request_timeout)
        self._status = info.status
        self._last_error = info.last_error
        if info.auto_idle_timeout_seconds is not None:
            self._auto_idle_timeout_seconds = info.auto_idle_timeout_seconds

    @classmethod
    def from_pool(
        cls,
        pool: str,
        *,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        auto_idle_timeout_seconds: int | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Claim a sandbox from a warm pool.

        This is the fastest way to get a sandbox: the backend adopts a
        pre-warmed pod, but does so asynchronously, so the claim starts out
        in phase Pending. Call :meth:`wait_until_ready` before using it.

        Args:
            pool: Name of the warm pool.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            auto_idle_timeout_seconds: Per-claim auto-idle override in seconds.
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A Sandbox instance, usually still starting up.

        Example:
            >>> sbx = Sandbox.from_pool("python-pool")
            >>> sbx.wait_until_ready()
            >>> sbx.run_code("print('Hello!')")
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxClient(config)
        try:
            info = client.claim_from_pool(
                pool, auto_idle_timeout_seconds=auto_idle_timeout_seconds
            )
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            status=info.status,
            pool=pool,
            auto_idle_timeout_seconds=(
                info.auto_idle_timeout_seconds
                if info.auto_idle_timeout_seconds is not None
                else auto_idle_timeout_seconds
            ),
        )

    @classmethod
    def list(
        cls,
        *,
        phase: str | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> list[Self]:
        """List all sandboxes in the workspace.

        Args:
            phase: Filter by phase (e.g. "Running", "Paused", "Pending").
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            List of ready-to-use Sandbox instances.

        Example:
            >>> sandboxes = Sandbox.list(phase="Paused")
            >>> for sbx in sandboxes:
            ...     print(f"{sbx.name}: {sbx.status}")
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxClient(config)
        try:
            infos = client.list()
        except Exception:
            client.close()
            raise

        # Close the temporary listing client — no longer needed.
        client.close()

        # Filter by phase if requested
        if phase is not None:
            infos = [i for i in infos if i.status.value == phase]

        if not infos:
            return []

        return cls._wrap_infos(infos, config)

    @classmethod
    def _wrap_infos(cls, infos: Sequence[SandboxInfo], config: Config) -> list[Self]:
        """Build Sandbox instances from listing results.

        Each Sandbox gets its own client so that ``kill()`` on one does not
        invalidate the others. The version check is skipped because the
        listing client already verified compatibility. If any construction
        fails, every client created here is closed — including the one whose
        owning instance never came into existence.
        """
        sandboxes: list[Self] = []
        try:
            for info in infos:
                client = SandboxClient(config, check_version=False)
                try:
                    sandbox = cls(
                        name=info.name,
                        workspace=info.workspace,
                        client=client,
                        status=info.status,
                        pool=info.pool,
                        image=info.image,
                        auto_idle_timeout_seconds=info.auto_idle_timeout_seconds,
                    )
                except Exception:
                    client.close()
                    raise
                sandboxes.append(sandbox)
        except Exception:
            for sbx in sandboxes:
                sbx._client.close()
            raise

        return sandboxes

    @classmethod
    def list_page(
        cls,
        *,
        limit: int = 25,
        continue_token: str | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> SandboxPage[Self]:
        """List one bounded page of sandboxes.

        One name-ordered listing covers every sandbox state. Pass
        ``continue_token`` from the previous page together with the same
        ``limit`` to fetch the next page; the token is an opaque keyset
        cursor.
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxClient(config)
        try:
            page = client.list_page(
                limit=limit,
                continue_token=continue_token,
            )
        finally:
            client.close()

        return SandboxPage(
            sandboxes=cls._wrap_infos(page.sandboxes, config),
            loaded=page.loaded,
            has_more=page.has_more,
            continue_token=page.continue_token,
        )

    @classmethod
    def get(
        cls,
        name: str,
        *,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Connect to an existing sandbox.

        Use this to interact with a sandbox that was created elsewhere
        (e.g., via the UI or another process).

        Args:
            name: Name of the existing sandbox.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A Sandbox instance connected to the existing sandbox.

        Example:
            >>> sbx = Sandbox.get("claim-abc123")
            >>> sbx.run_code("print('Hello!')")
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxClient(config)
        try:
            info = client.get(name)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            status=info.status,
            pool=info.pool,
            image=info.image,
            auto_idle_timeout_seconds=info.auto_idle_timeout_seconds,
        )

    # Alias: Sandbox.connect() is the same as Sandbox.get()
    connect = get

    @classmethod
    def create(
        cls,
        image: str,
        *,
        name: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        allow_internet_access: bool | None = None,
        auto_idle_timeout_seconds: int | None = None,
        env_vars: Sequence[EnvVarInput] | None = None,
        secret_refs: list[str] | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new sandbox directly.

        This has a cold start time of ~10-30 seconds. Use from_pool()
        for faster startup when possible.

        Args:
            image: Container image to use.
            name: Optional sandbox name (auto-generated if not provided).
            cpu: CPU resource request (e.g. '2'). If None, the backend default
                is used.
            memory: Memory resource request (e.g. '4Gi'). If None, the backend
                default is used.
            allow_internet_access: Whether the sandbox may reach the public
                internet. If None, the backend default is used.
            auto_idle_timeout_seconds: Per-sandbox auto-idle override in seconds.
            env_vars: Environment variables to inject into the sandbox. Each
                entry is an :class:`EnvVar` or an equivalent
                ``{"name": ..., "value": ...}`` mapping.
            secret_refs: Names of workspace secrets to mount into the sandbox.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A Sandbox instance (may need time to become ready).

        Example:
            >>> sbx = Sandbox.create(
            ...     image="pk-sandbox:python-datascience",
            ...     cpu="2",
            ...     memory="4Gi",
            ...     allow_internet_access=True,
            ...     env_vars=[{"name": "FOO", "value": "bar"}],
            ...     secret_refs=["openai-key"],
            ... )
            >>> sbx.wait_until_ready()
            >>> sbx.run_code("print('Ready!')")
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxClient(config)
        try:
            info = client.create(
                image=image,
                name=name,
                cpu=cpu,
                memory=memory,
                allow_internet_access=allow_internet_access,
                auto_idle_timeout_seconds=auto_idle_timeout_seconds,
                env_vars=env_vars,
                secret_refs=secret_refs,
            )
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            status=info.status,
            image=image,
            auto_idle_timeout_seconds=(
                info.auto_idle_timeout_seconds
                if info.auto_idle_timeout_seconds is not None
                else auto_idle_timeout_seconds
            ),
        )

    @staticmethod
    def _build_config(
        api_url: str | None,
        workspace: str | None,
        user_id: str | None,
        api_key: str | None,
        timeout: int | None,
    ) -> Config:
        """Build configuration from explicit params and environment."""
        kwargs: dict = {}
        if api_url is not None:
            kwargs["api_url"] = api_url
        if workspace is not None:
            kwargs["workspace"] = workspace
        if user_id is not None:
            kwargs["user_id"] = user_id
        if api_key is not None:
            kwargs["api_key"] = api_key
        if timeout is not None:
            kwargs["timeout"] = timeout
        return Config(**kwargs)

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit context manager - kills the sandbox.

        If the with-block raised an exception, cleanup errors are suppressed
        to avoid masking the original error. If the with-block succeeded,
        cleanup errors are propagated so failures are visible.
        """
        try:
            self.kill()
        except Exception:
            if exc_type is not None:
                # Don't mask the original exception from the with-block
                return False
            # No exception from with-block: propagate cleanup failure
            raise
        return False  # Never suppress exceptions from the with-block

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"Sandbox(name={self._name!r}, "
            f"workspace={self._workspace!r}, "
            f"status={self._status.value!r})"
        )
