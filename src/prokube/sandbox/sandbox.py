"""Main Sandbox class for interacting with prokube sandboxes."""

from __future__ import annotations

from typing import Self

from prokube.common.config import Config
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
        namespace: str,
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
            namespace: Kubernetes namespace.
            client: Sandbox API client.
            status: Current sandbox status.
            pool: WarmPool name if claimed from pool.
            image: Container image if created directly.
        """
        self._name = name
        self._namespace = namespace
        self._client = client
        self._status = status
        self._pool = pool
        self._image = image

        # Initialize helpers
        self._commands = CommandRunner(client, name)
        self._files = FileManager(client, name)
        self._code = CodeRunner(client, name)

    @property
    def name(self) -> str:
        """Get the sandbox name."""
        return self._name

    @property
    def namespace(self) -> str:
        """Get the Kubernetes namespace."""
        return self._namespace

    @property
    def status(self) -> str:
        """Get the current status."""
        return self._status.value

    @property
    def commands(self) -> CommandRunner:
        """Get the command runner for shell commands."""
        return self._commands

    @property
    def files(self) -> FileManager:
        """Get the file manager for file operations."""
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
        return self._code.run(code, language=language, timeout=timeout)

    def kill(self) -> None:
        """Destroy the sandbox immediately.

        After calling this method, the sandbox cannot be used anymore.
        """
        self._client.delete(self._name)
        self._client.close()
        self._status = SandboxStatus.SUCCEEDED

    def refresh(self) -> None:
        """Refresh sandbox information from the API."""
        info = self._client.get(self._name)
        self._status = info.status

    @classmethod
    def from_pool(
        cls,
        pool: str,
        *,
        api_url: str | None = None,
        namespace: str | None = None,
        user_id: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Claim a sandbox from a warm pool.

        This is the fastest way to get a sandbox - typically <100ms.
        The sandbox is pre-warmed and ready to use immediately.

        Args:
            pool: Name of the warm pool.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            namespace: Namespace (default: from PROKUBE_NAMESPACE env var).
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
            namespace=namespace,
            user_id=user_id,
            timeout=timeout,
        )
        client = SandboxClient(config)
        info = client.claim_from_pool(pool)

        return cls(
            name=info.name,
            namespace=info.namespace,
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
        namespace: str | None = None,
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
            namespace: Namespace (default: from PROKUBE_NAMESPACE env var).
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
            namespace=namespace,
            user_id=user_id,
            timeout=timeout,
        )
        client = SandboxClient(config)
        info = client.create(image=image, name=name)

        return cls(
            name=info.name,
            namespace=info.namespace,
            client=client,
            status=info.status,
            image=image,
        )

    @staticmethod
    def _build_config(
        api_url: str | None,
        namespace: str | None,
        user_id: str | None,
        timeout: int | None,
    ) -> Config:
        """Build configuration from explicit params and environment."""
        kwargs: dict = {}
        if api_url is not None:
            kwargs["api_url"] = api_url
        if namespace is not None:
            kwargs["namespace"] = namespace
        if user_id is not None:
            kwargs["user_id"] = user_id
        if timeout is not None:
            kwargs["timeout"] = timeout
        return Config(**kwargs)

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager - kills the sandbox."""
        self.kill()

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"Sandbox(name={self._name!r}, "
            f"namespace={self._namespace!r}, "
            f"status={self._status.value!r})"
        )
