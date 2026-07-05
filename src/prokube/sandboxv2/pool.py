"""SandboxV2Pool: a warm pool of pre-hibernated Firecracker sandboxes.

Mirrors the v1 :class:`prokube.sandbox.SandboxPool` public surface, adapted to
the v2 (Firecracker) backend. A pool maintains ``size`` pre-hibernated members;
:meth:`SandboxV2.from_pool` (or :meth:`SandboxV2Pool.claim`) turns a warm member
into a running sandbox via a fast VM resume rather than a cold boot.

The pool member ``template`` is a full v2 sandbox create spec (the same knobs as
:meth:`SandboxV2.create`); the backend forces ``runtimeClassName: fc-host`` and
owns each member's ``operatingMode``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError
from prokube.sandboxv2.client import SandboxV2Client
from prokube.sandboxv2.models import (
    CreateSandboxV2Request,
    HibernatedPoolMember,
)

if TYPE_CHECKING:
    from prokube.sandboxv2.sandbox import SandboxV2


class SandboxV2Pool:
    """A warm pool of pre-hibernated Firecracker sandboxes.

    Example:
        >>> pool = SandboxV2Pool.create(
        ...     name="python-pool",
        ...     size=3,
        ...     image="pk-sandbox-base",
        ...     vcpus=2,
        ...     mem_mib=2048,
        ... )
        >>> print(pool.name, pool.size, pool.ready_members)
        >>>
        >>> # Claim a warm member (fast resume)
        >>> sbx = pool.claim()
        >>> sbx.wait_until_ready()
        >>>
        >>> for p in SandboxV2Pool.list():
        ...     print(f"{p.name}: {p.ready_members}/{p.size}")
        >>>
        >>> pool.delete()
    """

    def __init__(
        self,
        name: str,
        namespace: str,
        client: SandboxV2Client,
        size: int = 0,
        ready_members: int = 0,
        members: list[HibernatedPoolMember] | None = None,
        image: str | None = None,
        runtime_class: str | None = None,
    ) -> None:
        """Initialize a SandboxV2Pool instance.

        Note: use :meth:`create`, :meth:`get`, or :meth:`list` instead of the
        constructor.
        """
        self._name = name
        self._namespace = namespace
        self._client = client
        self._size = size
        self._ready_members = ready_members
        self._members = members or []
        self._image = image
        self._runtime_class = runtime_class
        self._deleted = False

    @property
    def name(self) -> str:
        """The pool name."""
        return self._name

    @property
    def namespace(self) -> str:
        """The Kubernetes namespace (v1's ``workspace``)."""
        return self._namespace

    @property
    def size(self) -> int:
        """Desired number of warm members (spec.size)."""
        return self._size

    @property
    def ready_members(self) -> int:
        """Number of claimable warm members (status.readyMembers)."""
        return self._ready_members

    @property
    def members(self) -> list[HibernatedPoolMember]:
        """Current pool members (status.members)."""
        return self._members

    @property
    def image(self) -> str | None:
        """The template base OCI image."""
        return self._image

    @property
    def runtime_class(self) -> str | None:
        """The template runtime class (always ``fc-host`` for pools)."""
        return self._runtime_class

    def _check_not_deleted(self) -> None:
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
        """Delete this pool (idempotent). The object should not be reused."""
        if self._deleted:
            return
        self._client.delete_pool(self._name)
        self._deleted = True
        self._client.close()

    def refresh(self) -> None:
        """Refresh pool information from the API."""
        self._check_not_deleted()
        info = self._client.get_pool(self._name)
        self._size = info.size
        self._ready_members = info.ready_members
        self._members = info.members
        self._image = info.image
        self._runtime_class = info.runtime_class_name

    def claim(self) -> SandboxV2:
        """Claim a ready member from this pool (fast resume, not cold boot).

        Returns:
            A :class:`SandboxV2` bound to the claimed member. Call
            ``wait_until_ready()`` before use.

        Raises:
            SandboxError: If the pool has no ready member to claim (HTTP 409).
        """
        # Imported lazily to avoid a circular import (sandbox imports nothing
        # from pool at module load, but pool binds a SandboxV2 here).
        from prokube.sandboxv2.sandbox import SandboxV2

        self._check_not_deleted()
        # Each claimed sandbox owns its own client so the pool's client (and
        # other claimed sandboxes) survive when one sandbox is killed.
        client = SandboxV2Client(self._client.config, check_version=False)
        try:
            info = client.claim(self._name)
        except Exception:
            client.close()
            raise
        return SandboxV2(
            name=info.name,
            namespace=info.namespace,
            client=client,
            status=info.status,
            image=info.image,
            runtime_class=info.runtime_class,
        )

    # -- constructors ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        size: int,
        image: str | None = None,
        *,
        resources: dict | None = None,
        vcpus: int | None = None,
        mem_mib: int | None = None,
        egress: bool = False,
        terminal: bool = True,
        volumes: list[dict] | None = None,
        volume_mounts: list[dict] | None = None,
        image_pull_secrets: list[str] | None = None,
        workspace_size: str | None = None,
        target_node: str | None = None,
        namespace: str | None = None,
        api_url: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new warm pool of pre-hibernated Firecracker sandboxes.

        Args:
            name: Pool name.
            size: Desired number of warm (pre-hibernated) members.
            image: Base OCI image for members (defaults to pk-sandbox-base).
            resources: ``{"vcpus": int, "mem_mib": int}`` shorthand.
            vcpus: Guest vCPUs (overrides ``resources['vcpus']``).
            mem_mib: Guest memory in MiB (overrides ``resources['mem_mib']``).
            egress: Whether members may reach the cluster/internet.
            terminal: Inject a ttyd Terminal into members.
            volumes: ``spec.volumes`` pass-through (CR-shaped dicts).
            volume_mounts: ``spec.volumeMounts`` pass-through (CR-shaped dicts).
            image_pull_secrets: Registry pull secret names.
            workspace_size: Ephemeral /workspace volume size (e.g. "10Gi").
            target_node: Pin members to a node.
            namespace: Kubernetes namespace (maps to v1's ``workspace``).
            api_url / user_id / api_key / timeout: Connection overrides.

        Note: pools are ``fc-host`` only; the backend forces the member template
        to ``runtimeClassName: fc-host`` and owns ``operatingMode``.
        """
        if resources:
            vcpus = vcpus if vcpus is not None else resources.get("vcpus")
            mem_mib = mem_mib if mem_mib is not None else resources.get("mem_mib")

        config = cls._build_config(
            api_url=api_url,
            namespace=namespace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        # The template's own name is ignored by the backend (members are named
        # by the controller); reuse the pool name to satisfy validation.
        template = CreateSandboxV2Request(
            name=name,
            image=image,
            runtime_class_name="fc-host",
            vcpus=vcpus,
            mem_mib=mem_mib,
            egress=egress,
            terminal=terminal,
            volumes=volumes,
            volume_mounts=volume_mounts,
            image_pull_secrets=image_pull_secrets,
            workspace_size=workspace_size,
            target_node=target_node,
        )
        client = SandboxV2Client(config)
        try:
            info = client.create_pool(name=name, size=size, template=template)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            namespace=info.namespace,
            client=client,
            size=info.size,
            ready_members=info.ready_members,
            members=info.members,
            image=info.image or image,
            runtime_class=info.runtime_class_name or "fc-host",
        )

    @classmethod
    def get(
        cls,
        name: str,
        *,
        namespace: str | None = None,
        api_url: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Get an existing warm pool by name."""
        config = cls._build_config(
            api_url=api_url,
            namespace=namespace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            info = client.get_pool(name)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            namespace=info.namespace,
            client=client,
            size=info.size,
            ready_members=info.ready_members,
            members=info.members,
            image=info.image,
            runtime_class=info.runtime_class_name,
        )

    @classmethod
    def list(
        cls,
        *,
        namespace: str | None = None,
        api_url: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> list[Self]:
        """List all warm pools in the namespace."""
        config = cls._build_config(
            api_url=api_url,
            namespace=namespace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            infos = client.list_pools()
        except Exception:
            client.close()
            raise
        client.close()

        if not infos:
            return []

        # Each pool gets its own client so delete() on one does not invalidate
        # the others. Skip version checks (verified earlier where applicable).
        pools: list[Self] = []
        try:
            for info in infos:
                pools.append(
                    cls(
                        name=info.name,
                        namespace=info.namespace,
                        client=SandboxV2Client(config, check_version=False),
                        size=info.size,
                        ready_members=info.ready_members,
                        members=info.members,
                        image=info.image,
                        runtime_class=info.runtime_class_name,
                    )
                )
        except Exception:
            for p in pools:
                p._client.close()
            raise
        return pools

    @staticmethod
    def _build_config(
        api_url: str | None,
        namespace: str | None,
        user_id: str | None,
        api_key: str | None,
        timeout: int | None,
    ) -> Config:
        """Build configuration, mapping ``namespace`` to Config.workspace."""
        kwargs: dict = {}
        if api_url is not None:
            kwargs["api_url"] = api_url
        if namespace is not None:
            kwargs["workspace"] = namespace
        if user_id is not None:
            kwargs["user_id"] = user_id
        if api_key is not None:
            kwargs["api_key"] = api_key
        if timeout is not None:
            kwargs["timeout"] = timeout
        return Config(**kwargs)

    def __repr__(self) -> str:
        return (
            f"SandboxV2Pool(name={self._name!r}, "
            f"namespace={self._namespace!r}, "
            f"size={self._size}, ready_members={self._ready_members})"
        )
