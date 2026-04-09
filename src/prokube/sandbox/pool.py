"""SandboxPool class for managing warm pool lifecycle."""

from __future__ import annotations

import logging
import sys
import time

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError
from prokube.sandbox.pool_client import PoolClient

logger = logging.getLogger(__name__)


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
        """
        self._name = name
        self._workspace = workspace
        self._client = client
        self._replicas = replicas
        self._ready_replicas = ready_replicas
        self._image = image
        self._cpu = cpu
        self._memory = memory
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
        env_vars: list[dict[str, str]] | None = None,
        secret_refs: list[str] | None = None,
        wait_until_ready: bool = True,
        ready_timeout: int = 300,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new sandbox pool.

        By default this blocks until the pool is usable end-to-end: it polls
        until ``ready_replicas >= pool_size`` and then claims a single sandbox
        from the fresh pool, runs the same Jupyter-kernel warmup probe that
        :meth:`Sandbox.wait_until_ready` uses, and kills it. This eliminates
        the cold-kernel race where a freshly-created pool's pods are
        physically ``Running`` but their ipykernel is still cold (~1-2s), so
        the very first ``Sandbox.from_pool(...).run_code(...)`` after a fresh
        ``SandboxPool.create(...)`` could race and return empty stdout.

        The single probe's wall-clock is itself the cold-kernel window, so by
        the time the probe returns the other pool pods have had a similar
        amount of wall-clock time to warm up naturally. This is "Option A"
        from issue #24 — probabilistically safe and cheap.

        Opt out with ``wait_until_ready=False`` to preserve the previous
        instant-return behaviour. This is the escape hatch for callers who
        explicitly don't want the wait (e.g. tests, or callers that will do
        their own readiness handling).

        ``Sandbox.from_pool`` is intentionally unchanged by this method — the
        hot path of claiming from an already-warm pool stays fast.

        Args:
            name: Pool name.
            image: Container image for sandboxes in the pool.
            pool_size: Number of warm sandboxes to maintain.
            cpu: CPU resource request (e.g. '2').
            memory: Memory resource request (e.g. '4Gi').
            allow_internet_access: Whether sandboxes in the pool may reach the
                public internet. If None, the backend default is used.
            env_vars: Environment variables to inject into pool sandboxes. Each
                entry is a ``{"name": ..., "value": ...}`` dict.
            secret_refs: Names of workspace secrets to mount into pool
                sandboxes.
            wait_until_ready: If True (default), block until the pool is ready
                and warm one pod via the cold-kernel probe before returning.
                If False, return immediately after the pool CR is created.
            ready_timeout: Maximum seconds to wait for the pool to become
                ready and for the warmup probe to finish. Applied as a single
                overall budget across both phases. On timeout (or any other
                failure during warmup) the method logs a warning and returns
                the pool anyway — the warmup is purely best-effort. Note this
                is different from :meth:`Sandbox.wait_until_ready`, which
                *raises* :class:`SandboxTimeoutError` on a pod-not-ready
                timeout; only its kernel warmup probe phase is best-effort.
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
            >>> # Pool is guaranteed warm — no cold-kernel race on first claim
            >>> sbx = Sandbox.from_pool("my-pool")
            >>> sbx.run_code("print('hello')").stdout.strip()  # "hello"
        """
        # Local name for the flag so there's no confusion with the similarly
        # named Sandbox.wait_until_ready method used below.
        should_warm = wait_until_ready

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
                env_vars=env_vars,
                secret_refs=secret_refs,
            )
        except Exception:
            client.close()
            raise

        pool = cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            replicas=info.replicas,
            ready_replicas=info.ready_replicas,
            image=info.image,
            cpu=info.cpu,
            memory=info.memory,
        )

        if should_warm:
            pool._warm_one_pod(
                ready_timeout=ready_timeout,
                api_url=api_url,
                workspace=workspace,
                user_id=user_id,
                api_key=api_key,
                timeout=timeout,
            )

        return pool

    def _warm_one_pod(
        self,
        *,
        ready_timeout: int,
        api_url: str | None,
        workspace: str | None,
        user_id: str | None,
        api_key: str | None,
        timeout: int | None,
    ) -> None:
        """Wait for the pool to be ready, then warm one pod end-to-end.

        Phase 1: poll ``self.refresh()`` until
        ``ready_replicas >= self._replicas`` (the *actual* desired count from
        the backend, which may differ from the requested ``pool_size`` if the
        backend clamps or applies defaults) or ``ready_timeout`` is exhausted.

        Phase 2: claim one sandbox from the pool, call ``wait_until_ready``
        on it (which runs the cold-kernel probe), then kill it.

        Any failure in either phase is swallowed with a warning — this is a
        best-effort warmup. The pool CR itself has already been created before
        this is called, so the caller still gets a usable :class:`SandboxPool`
        handle even if the warmup cannot complete.
        """
        # Local import to avoid a circular import at module load time:
        # sandbox.py imports nothing from pool.py, but pool.py -> sandbox.py
        # would pull the SandboxClient stack eagerly and complicate test
        # isolation for pool-only tests. Importing here keeps the hot path
        # (from_pool) cold-import-free for users who never call create().
        from prokube.sandbox.sandbox import Sandbox

        deadline = time.monotonic() + ready_timeout
        poll_interval = 2.0

        # Phase 1: pool readiness.
        try:
            while True:
                try:
                    self.refresh()
                except Exception as exc:  # noqa: BLE001 - best-effort
                    logger.warning(
                        "SandboxPool %s warmup: refresh failed (%s); "
                        "skipping warmup probe",
                        self._name,
                        exc,
                    )
                    return
                # Compare against the backend-reported desired replica count
                # (self._replicas), not the requested pool_size argument: if
                # the backend clamps or applies a default, the request value
                # may never be reached even though the pool is fully ready.
                if self._ready_replicas >= self._replicas:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "SandboxPool %s warmup: pool did not reach "
                        "ready_replicas>=%d within %ds (have %d); "
                        "skipping warmup probe",
                        self._name,
                        self._replicas,
                        ready_timeout,
                        self._ready_replicas,
                    )
                    return
                time.sleep(min(poll_interval, remaining))
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning(
                "SandboxPool %s warmup: unexpected error while waiting for "
                "pool readiness (%s); skipping warmup probe",
                self._name,
                exc,
            )
            return

        # Phase 2: claim + probe + kill one sandbox. Reuse the same connection
        # parameters the pool was created with so the probe hits the same
        # backend / workspace.
        #
        # Sandbox.wait_until_ready needs an integer second timeout. If less
        # than 1 second remains we cannot probe without overrunning the
        # caller's ready_timeout, so we skip the probe entirely (consistent
        # with how Sandbox._warmup_kernel handles its own remaining budget).
        remaining = deadline - time.monotonic()
        if remaining < 1:
            logger.warning(
                "SandboxPool %s warmup: no time left after readiness poll; "
                "skipping warmup probe",
                self._name,
            )
            return

        sbx = None
        try:
            sbx = Sandbox.from_pool(
                self._name,
                api_url=api_url,
                workspace=workspace,
                user_id=user_id,
                api_key=api_key,
                timeout=timeout,
            )
            # Sandbox.wait_until_ready runs the cold-kernel warmup probe and
            # is itself best-effort on probe timeout — it logs and returns.
            # Cap its budget by the remaining pool-level deadline so it can
            # never exceed the caller's ready_timeout.
            probe_budget = int(deadline - time.monotonic())
            if probe_budget < 1:
                logger.warning(
                    "SandboxPool %s warmup: less than 1s remains before "
                    "claiming the probe sandbox; skipping warmup probe",
                    self._name,
                )
            else:
                sbx.wait_until_ready(timeout=probe_budget)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning(
                "SandboxPool %s warmup: probe sandbox failed (%s); "
                "pool is returned anyway",
                self._name,
                exc,
            )
        finally:
            if sbx is not None:
                try:
                    sbx.kill()
                except Exception as exc:  # noqa: BLE001 - best-effort
                    logger.warning(
                        "SandboxPool %s warmup: failed to kill probe "
                        "sandbox %s (%s); leaking one claim",
                        self._name,
                        sbx.name,
                        exc,
                    )
                    # kill() leaves the underlying HTTP client open if the
                    # delete request fails (kill() only closes the client
                    # after a successful delete). Close it explicitly here
                    # to avoid leaking connections in long-lived processes.
                    try:
                        sbx._client.close()
                    except Exception:  # noqa: BLE001
                        pass

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
