"""Main Sandbox class for interacting with prokube sandboxes."""

from __future__ import annotations

import logging
import sys
import time
import uuid

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError, SandboxTimeoutError
from prokube.sandbox.client import SandboxClient
from prokube.sandbox.code import CodeRunner
from prokube.sandbox.commands import CommandRunner
from prokube.sandbox.files import FileManager
from prokube.sandbox.models import CodeResult, SandboxStatus

logger = logging.getLogger(__name__)


class Sandbox:
    """A sandbox environment for executing code and commands.

    Sandboxes provide isolated environments for running code safely.
    They can be created directly or claimed from a warm pool for faster startup.

    Example:
        >>> # Claim from warm pool (instant, <100ms)
        >>> sbx = Sandbox.from_pool("python-pool")
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
        >>> # Cleanup
        >>> sbx.kill()

    Context Manager:
        >>> with Sandbox.from_pool("python-pool") as sbx:
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
        """
        self._name = name
        self._workspace = workspace
        self._client = client
        self._status = status
        self._pool = pool
        self._image = image
        self._killed = False
        self._skip_next_warmup = False

        # Initialize helpers with killed-state check callback
        self._commands = CommandRunner(client, name, self._check_not_killed)
        self._files = FileManager(client, name, self._check_not_killed)
        self._code = CodeRunner(client, name, self._check_not_killed)

    def _check_not_killed(self) -> None:
        """Raise error if sandbox has been killed."""
        if self._killed:
            raise SandboxError(
                f"Sandbox {self._name} has been killed and cannot be used anymore"
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

    def pause(self) -> None:
        """Pause the sandbox. Frees compute resources.

        Preserves: /workspace (working directory) and /home/agent (HOME, pip --user, dotfiles).
        Lost: running processes, apt-installed system packages, /tmp.

        Raises:
            SandboxError: If sandbox is not in Running state.
        """
        self._check_not_killed()
        self._client.pause(self._name)
        self._status = SandboxStatus.PAUSED
        # Pausing deletes the underlying pod, so any existing Jupyter session
        # is no longer valid. Reset so next run_code() starts a fresh kernel.
        self._code.reset_session()

    def resume(self) -> None:
        """Resume a paused sandbox.

        A new pod starts with the same PVC mounts at /workspace and /home/agent.
        If /home/agent/.sandbox-restore.sh exists, it runs automatically on
        startup to reinstall system packages.

        Raises:
            SandboxError: If sandbox is not in Paused state.
        """
        self._check_not_killed()
        info = self._client.resume(self._name)
        self._status = info.status
        self._skip_next_warmup = info.resumed_from_pool
        # New pod means previous Jupyter session is invalid.
        self._code.reset_session()

    def wait_until_ready(self, timeout: int = 120) -> None:
        """Block until sandbox phase is Running. Useful after resume().

        Args:
            timeout: Maximum seconds to wait (default: 120).

        Raises:
            SandboxTimeoutError: If sandbox does not become Running within timeout.
            SandboxError: If the sandbox enters a terminal state (Failed/Succeeded)
                while waiting for it to become ready.
        """
        self._check_not_killed()
        poll_interval = 0.5
        deadline = time.monotonic() + timeout
        while True:
            self.refresh()
            if self._status == SandboxStatus.RUNNING:
                if self._skip_next_warmup:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._skip_next_warmup = False
                        return
                    time.sleep(min(poll_interval, remaining))
                    self.refresh()
                    if self._status == SandboxStatus.RUNNING:
                        self._skip_next_warmup = False
                        return
                    continue
                self._warmup_kernel(deadline)
                return
            if self._status in (SandboxStatus.FAILED, SandboxStatus.SUCCEEDED):
                raise SandboxError(
                    f"Sandbox {self._name} entered terminal state "
                    f"{self._status.value!r} while waiting for it to become ready"
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
        """Probe the Jupyter kernel until it echoes a unique marker back.

        After the pod reaches Running, ipykernel still needs ~1–2s before its
        first ``execute_request`` produces visible iopub stdout. During that
        window, execd's ``exec`` sub-resource returns successfully but the
        stdout never reaches the SSE stream, so the first user ``run_code()``
        call races the cold kernel pipeline and silently returns empty output.

        This method hides that race by running a tiny ``print(<marker>)``
        probe in a loop until the stdout matches, proving the kernel pipeline
        is end-to-end live. The probe is bounded by ``deadline`` (the same
        deadline used by :meth:`wait_until_ready`), so it can never exceed the
        caller's overall timeout budget. If the deadline is reached without
        success, a warning is logged and the method returns without raising —
        the user may still get useful results, and we don't want to block
        ``create`` when the workaround is only partially working.

        Notes:
            * The marker is per-call (``uuid4().hex``) to avoid collisions
              with any user code that happens to print a similar literal.
            * The probe must NOT pass ``reset_session=True`` or an explicit
              ``session_id``; it exercises the same code path the user will.

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
            result = self.run_code(code, timeout=probe_timeout)
            if result.stdout.strip() == marker:
                return
            # Loop top will recompute remaining and exit if deadline passed.
            sleep_for = min(0.5, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)

    def kill(self) -> None:
        """Destroy the sandbox immediately.

        After calling this method, the sandbox cannot be used anymore.
        Any subsequent calls to run_code(), commands, or files will raise.

        If the delete request fails, an exception is raised and the sandbox
        remains usable so callers can retry or handle the failure.
        """
        if self._killed:
            return  # Already killed, nothing to do
        self._client.delete(self._name)
        # Only mark as killed and close client after successful delete
        self._status = SandboxStatus.SUCCEEDED
        self._killed = True
        self._client.close()

    def refresh(self) -> None:
        """Refresh sandbox information from the API."""
        self._check_not_killed()
        info = self._client.get(self._name)
        self._status = info.status

    @classmethod
    def from_pool(
        cls,
        pool: str,
        *,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Claim a sandbox from a warm pool.

        This is the fastest way to get a sandbox - typically <100ms.
        The sandbox is pre-warmed and ready to use immediately.

        Args:
            pool: Name of the warm pool.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A ready-to-use Sandbox instance.

        Example:
            >>> sbx = Sandbox.from_pool("python-pool")
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
            info = client.claim_from_pool(pool)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            status=info.status,
            pool=pool,
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

        # Each Sandbox gets its own client so that kill() on one
        # does not invalidate the others. Skip version check since
        # we already verified compatibility above.
        sandboxes: list[Self] = []
        try:
            for info in infos:
                sandboxes.append(
                    cls(
                        name=info.name,
                        workspace=info.workspace,
                        client=SandboxClient(config, check_version=False),
                        status=info.status,
                        pool=info.pool,
                        image=info.image,
                    )
                )
        except Exception:
            for sbx in sandboxes:
                sbx._client.close()
            raise

        return sandboxes

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
        env_vars: list[dict[str, str]] | None = None,
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
            env_vars: Environment variables to inject into the sandbox. Each
                entry is a ``{"name": ..., "value": ...}`` dict.
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
            >>> # Wait for sandbox to be ready
            >>> while sbx.status == "Pending":
            ...     time.sleep(1)
            ...     sbx.refresh()
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
