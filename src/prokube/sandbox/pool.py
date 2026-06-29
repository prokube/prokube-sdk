"""SandboxPool class for managing warm pool lifecycle."""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError
from prokube.sandbox.pool_client import PoolClient


class SandboxPool:
    """A warm pool of pre-provisioned sandboxes.

    Pools maintain a set of ready-to-claim sandboxes for fast startup.

    Example:
        >>> pool = SandboxPool.create(
        ...     name="python-pool",
        ...     image="pk-sandbox-base:latest",
        ...     pool_size=3,
        ...     cpu="2",
        ...     memory="4Gi",
        ... )
        >>> print(pool.name, pool.pool_size)
        >>>
        >>> # List all pools
        >>> for p in SandboxPool.list():
        ...     print(f"{p.name}: {p.ready_replicas}/{p.pool_size}")
        >>>
        >>> # Get a specific pool
        >>> pool = SandboxPool.get("python-pool")
        >>>
        >>> # Delete
        >>> pool.delete()
    """

    def __init__(
        self,
        name: str,
        workspace: str,
        client: PoolClient,
        replicas: int = 0,
        ready_replicas: int = 0,
        image: str | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        auto_idle_timeout_seconds: int | None = None,
    ) -> None:
        """Initialize a SandboxPool instance.

        Note: Use SandboxPool.create(), SandboxPool.get(), or
        SandboxPool.list() instead of calling this constructor directly.

        Args:
            name: Pool name.
            workspace: Workspace (Kubernetes namespace).
            client: Pool API client.
            replicas: Desired pool size.
            ready_replicas: Number of ready replicas.
            image: Container image.
            cpu: CPU resource request.
            memory: Memory resource request.
            auto_idle_timeout_seconds: Default auto-idle timeout in seconds.
        """
        self._name = name
        self._workspace = workspace
        self._client = client
        self._replicas = replicas
        self._ready_replicas = ready_replicas
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._auto_idle_timeout_seconds = auto_idle_timeout_seconds
        self._deleted = False

    @property
    def name(self) -> str:
        """Get the pool name."""
        return self._name

    @property
    def workspace(self) -> str:
        """Get the workspace (Kubernetes namespace)."""
        return self._workspace

    @property
    def pool_size(self) -> int:
        """Get the desired pool size."""
        return self._replicas

    @property
    def replicas(self) -> int:
        """Alias for pool_size."""
        return self._replicas

    @property
    def ready_replicas(self) -> int:
        """Get the number of ready replicas."""
        return self._ready_replicas

    @property
    def image(self) -> str | None:
        """Get the container image."""
        return self._image

    @property
    def cpu(self) -> str | None:
        """Get the CPU resource request."""
        return self._cpu

    @property
    def memory(self) -> str | None:
        """Get the memory resource request."""
        return self._memory

    @property
    def auto_idle_timeout_seconds(self) -> int | None:
        """Get the default auto-idle timeout for claimed sandboxes, if known."""
        return self._auto_idle_timeout_seconds

    def _check_not_deleted(self) -> None:
        """Raise if pool has been deleted."""
        if self._deleted:
            raise SandboxError(f"Pool '{self._name}' has been deleted")

    def close(self) -> None:
        """Close the underlying HTTP client without deleting the pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def delete(self) -> None:
        """Delete this pool.

        After calling this method, the pool object should not be reused.
        Calling delete() on an already-deleted pool is a no-op (idempotent).
        """
        if self._deleted:
            return
        self._client.delete_pool(self._name)
        self._deleted = True
        self._client.close()

    def refresh(self) -> None:
        """Refresh pool information from the API."""
        self._check_not_deleted()
        info = self._client.get_pool(self._name)
        self._replicas = info.replicas
        self._ready_replicas = info.ready_replicas
        self._image = info.image
        self._cpu = info.cpu
        self._memory = info.memory
        if info.auto_idle_timeout_seconds is not None:
            self._auto_idle_timeout_seconds = info.auto_idle_timeout_seconds

    @classmethod
    def create(
        cls,
        name: str,
        image: str,
        pool_size: int,
        cpu: str,
        memory: str,
        *,
        allow_internet_access: bool | None = None,
        auto_idle_timeout_seconds: int | None = None,
        env_vars: list[dict[str, str]] | None = None,
        secret_refs: list[str] | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new sandbox pool.

        Args:
            name: Pool name.
            image: Container image for sandboxes in the pool.
            pool_size: Number of warm sandboxes to maintain.
            cpu: CPU resource request (e.g. '2').
            memory: Memory resource request (e.g. '4Gi').
            allow_internet_access: Whether sandboxes in the pool may reach the
                public internet. If None, the backend default is used.
            auto_idle_timeout_seconds: Default auto-idle timeout in seconds for
                sandboxes claimed from this pool.
            env_vars: Environment variables to inject into pool sandboxes. Each
                entry is a ``{"name": ..., "value": ...}`` dict.
            secret_refs: Names of workspace secrets to mount into pool
                sandboxes.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A SandboxPool instance representing the created pool.

        Example:
            >>> pool = SandboxPool.create(
            ...     name="my-pool",
            ...     image="pk-sandbox-base:pr-5",
            ...     pool_size=3,
            ...     cpu="2",
            ...     memory="4Gi",
            ...     allow_internet_access=True,
            ...     env_vars=[{"name": "FOO", "value": "bar"}],
            ...     secret_refs=["openai-key"],
            ... )
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = PoolClient(config)
        try:
            info = client.create_pool(
                name=name,
                image=image,
                pool_size=pool_size,
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
            replicas=info.replicas,
            ready_replicas=info.ready_replicas,
            image=info.image,
            cpu=info.cpu,
            memory=info.memory,
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
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> list[Self]:
        """List all sandbox pools in the workspace.

        Args:
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            List of SandboxPool instances.

        Example:
            >>> pools = SandboxPool.list()
            >>> for p in pools:
            ...     print(f"{p.name}: {p.ready_replicas}/{p.pool_size}")
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = PoolClient(config)
        try:
            infos = client.list_pools()
        except Exception:
            client.close()
            raise

        # Close the temporary listing client -- no longer needed.
        client.close()

        if not infos:
            return []

        # Each SandboxPool gets its own client so that delete() on one
        # does not invalidate the others. Skip version checks for these
        # per-pool clients; compatibility was verified earlier when
        # applicable, and API key mode intentionally bypasses that check.
        pools: list[Self] = []
        try:
            for info in infos:
                pools.append(
                    cls(
                        name=info.name,
                        workspace=info.workspace,
                        client=PoolClient(config, check_version=False),
                        replicas=info.replicas,
                        ready_replicas=info.ready_replicas,
                        image=info.image,
                        cpu=info.cpu,
                        memory=info.memory,
                        auto_idle_timeout_seconds=info.auto_idle_timeout_seconds,
                    )
                )
        except Exception:
            for p in pools:
                p._client.close()
            raise

        return pools

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
        """Get an existing sandbox pool.

        Args:
            name: Pool name.
            api_url: API URL (default: from PROKUBE_API_URL env var).
            workspace: Workspace (default: from PROKUBE_WORKSPACE env var).
            user_id: User ID (default: from PROKUBE_USER_ID env var).
            api_key: API key for external access (default: from PROKUBE_API_KEY env var).
            timeout: Request timeout (default: from PROKUBE_TIMEOUT env var).

        Returns:
            A SandboxPool instance.

        Example:
            >>> pool = SandboxPool.get("my-pool")
            >>> print(pool.ready_replicas)
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = PoolClient(config)
        try:
            info = client.get_pool(name)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            replicas=info.replicas,
            ready_replicas=info.ready_replicas,
            image=info.image,
            cpu=info.cpu,
            memory=info.memory,
            auto_idle_timeout_seconds=info.auto_idle_timeout_seconds,
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

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"SandboxPool(name={self._name!r}, "
            f"pool_size={self._replicas}, "
            f"ready_replicas={self._ready_replicas})"
        )
