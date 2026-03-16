"""Main Sandbox class for interacting with prokube sandboxes."""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError
from prokube.sandbox.client import SandboxClient
from prokube.sandbox.code import CodeRunner
from prokube.sandbox.commands import CommandRunner
from prokube.sandbox.files import FileManager
from prokube.sandbox.models import CodeResult, SandboxStatus


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

        # Initialize helpers
        self._commands = CommandRunner(client, name)
        self._files = FileManager(client, name)
        self._code = CodeRunner(client, name)

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
    def create(
        cls,
        image: str,
        *,
        name: str | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new sandbox directly.

        This has a cold start time of ~10-30 seconds. Use from_pool()
        for faster startup when possible.

        Args:
            image: Container image to use.
            name: Optional sandbox name (auto-generated if not provided).
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A Sandbox instance (may need time to become ready).

        Example:
            >>> sbx = Sandbox.create(image="pk-sandbox:python-datascience")
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
            timeout=timeout,
        )
        client = SandboxClient(config)
        try:
            info = client.create(image=image, name=name)
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
