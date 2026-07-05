"""Main SandboxV2 class for Firecracker (Sandbox v2) microVMs.

Mirrors the v1 :class:`prokube.sandbox.Sandbox` public surface, adapted to the
v2 backend: creation takes a ``runtime_class`` (``fc-host`` / ``fc-pod``) and v2
knobs (resources, egress, volumes), v1's ``workspace`` maps to ``namespace``,
and the warm pool is a FirecrackerHibernatedPool (:meth:`SandboxV2.from_pool`
claims a pre-hibernated member; see :class:`prokube.sandboxv2.pool.SandboxV2Pool`).

The stateful code / shell command / file helpers are the *same* v1 classes
(:class:`CodeRunner` / :class:`CommandRunner` / :class:`FileManager`) — they are
duck-typed against the client method surface, which :class:`SandboxV2Client`
reproduces exactly.
"""

from __future__ import annotations

import sys
import time

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError, SandboxTimeoutError
from prokube.sandbox.code import CodeRunner
from prokube.sandbox.commands import CommandRunner
from prokube.sandbox.files import FileManager
from prokube.sandbox.models import CodeResult
from prokube.sandboxv2.client import SandboxV2Client
from prokube.sandboxv2.models import SandboxV2Status


class SandboxV2:
    """A Firecracker microVM sandbox for executing code and commands.

    Example:
        >>> sbx = SandboxV2.create(
        ...     image="pk-sandbox-base",
        ...     runtime_class="fc-host",
        ...     resources={"vcpus": 2, "mem_mib": 2048},
        ...     egress=False,
        ... )
        >>> sbx.wait_until_ready()
        >>> print(sbx.run_code("print(2 + 2)").stdout)
        >>> sbx.commands.run("pip install numpy")
        >>> sbx.files.write("/workspace/x.txt", "hello")
        >>> sbx.kill()

    Context manager:
        >>> with SandboxV2.create(image="pk-sandbox-base") as sbx:
        ...     sbx.wait_until_ready()
        ...     print(sbx.run_code("print(42)").stdout)
    """

    def __init__(
        self,
        name: str,
        namespace: str,
        client: SandboxV2Client,
        status: SandboxV2Status = SandboxV2Status.PENDING,
        image: str | None = None,
        runtime_class: str | None = None,
    ) -> None:
        """Initialize a SandboxV2 instance.

        Note: use :meth:`create` or :meth:`get` instead of the constructor.
        """
        self._name = name
        self._namespace = namespace
        self._client = client
        self._status = status
        self._image = image
        self._runtime_class = runtime_class
        self._killed = False

        self._commands = CommandRunner(client, name, self._check_not_killed)
        self._files = FileManager(client, name, self._check_not_killed)
        self._code = CodeRunner(client, name, self._check_not_killed)

    def _check_not_killed(self) -> None:
        if self._killed:
            raise SandboxError(
                f"Sandbox {self._name} has been killed and cannot be used anymore"
            )

    @property
    def name(self) -> str:
        """The sandbox name."""
        return self._name

    @property
    def namespace(self) -> str:
        """The Kubernetes namespace."""
        return self._namespace

    @property
    def runtime_class(self) -> str | None:
        """The runtime class (fc-host | fc-pod)."""
        return self._runtime_class

    @property
    def status(self) -> str:
        """The last-known phase (does not refresh)."""
        return self._status.value

    @property
    def phase(self) -> str:
        """Current phase, refreshed from the API."""
        self.refresh()
        return self._status.value

    @property
    def commands(self) -> CommandRunner:
        """Runner for shell commands."""
        self._check_not_killed()
        return self._commands

    @property
    def files(self) -> FileManager:
        """Manager for guest file operations."""
        self._check_not_killed()
        return self._files

    def run_code(
        self,
        code: str,
        language: str = "python",
        timeout: int = 300,
    ) -> CodeResult:
        """Execute code in the guest Jupyter kernel (stateful)."""
        self._check_not_killed()
        return self._code.run(code, language=language, timeout=timeout)

    def reset_session(self) -> None:
        """Reset the Jupyter kernel session for the next ``run_code``."""
        self._check_not_killed()
        self._code.reset_session()

    @property
    def session_id(self) -> str | None:
        """Current Jupyter session ID, if any."""
        return self._code.session_id

    def pause(self) -> None:
        """Pause the sandbox (native VM snapshot to shared storage).

        Raises:
            SandboxError: If the sandbox is not in Running state (HTTP 409).
        """
        self._check_not_killed()
        info = self._client.pause(self._name)
        self._status = (
            info.status
            if info.status != SandboxV2Status.UNKNOWN
            else SandboxV2Status.PAUSED
        )
        self._code.reset_session()

    def resume(self) -> None:
        """Resume a paused sandbox (native VM restore).

        Raises:
            SandboxError: If the sandbox is not in Paused state (HTTP 409).
        """
        self._check_not_killed()
        info = self._client.resume(self._name)
        self._status = info.status
        self._code.reset_session()

    def wait_until_ready(self, timeout: int = 120) -> None:
        """Block until the sandbox phase is Running.

        Args:
            timeout: Maximum seconds to wait (default: 120).

        Raises:
            SandboxTimeoutError: If it does not become Running in time.
            SandboxError: If it enters the Failed terminal state while waiting.
        """
        self._check_not_killed()
        poll_interval = 2
        deadline = time.monotonic() + timeout
        while True:
            self.refresh()
            if self._status == SandboxV2Status.RUNNING:
                return
            if self._status == SandboxV2Status.FAILED:
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

    def kill(self) -> None:
        """Destroy the sandbox immediately.

        After this the sandbox cannot be used. If the delete request fails, an
        exception is raised and the sandbox remains usable so callers can retry.
        """
        if self._killed:
            return
        self._client.delete(self._name)
        self._status = SandboxV2Status.UNKNOWN
        self._killed = True
        self._client.close()

    def refresh(self) -> None:
        """Refresh sandbox information from the API."""
        self._check_not_killed()
        info = self._client.get(self._name)
        self._status = info.status
        if info.runtime_class is not None:
            self._runtime_class = info.runtime_class
        if info.image is not None:
            self._image = info.image

    # -- constructors ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        image: str | None = None,
        *,
        runtime_class: str = "fc-host",
        name: str | None = None,
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
        operating_mode: str | None = None,
        manifest: dict | None = None,
        namespace: str | None = None,
        api_url: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new Firecracker sandbox.

        Args:
            image: Base OCI image. If None, the backend default (pk-sandbox-base)
                is used.
            runtime_class: ``fc-host`` (VM, default) or ``fc-pod`` (pod-hosted).
                Maps to ``spec.runtimeClassName``.
            name: Optional sandbox name (auto-generated if not provided).
            resources: Optional ``{"vcpus": int, "mem_mib": int}`` shorthand.
                Explicit ``vcpus`` / ``mem_mib`` kwargs take precedence.
            vcpus: Guest vCPUs (overrides ``resources['vcpus']``).
            mem_mib: Guest memory in MiB (overrides ``resources['mem_mib']``).
            egress: Whether the microVM may reach the cluster/internet
                (default: False — isolated).
            terminal: Inject a ttyd Terminal (:7681) into the guest.
            volumes: ``spec.volumes`` pass-through (CR-shaped dicts).
            volume_mounts: ``spec.volumeMounts`` pass-through (CR-shaped dicts).
            image_pull_secrets: Registry pull secret names.
            workspace_size: Default ephemeral /workspace size (e.g. "10Gi").
            target_node: Pin the microVM to a node.
            operating_mode: ``Running`` or ``Hibernated``.
            manifest: Full FirecrackerSandbox object; wins over structured knobs.
            namespace: Kubernetes namespace (maps to v1's ``workspace``).
            api_url: API URL (default: PROKUBE_API_URL env var).
            user_id: User ID (default: PROKUBE_USER_ID env var).
            api_key: API key (default: PROKUBE_API_KEY env var).
            timeout: Request timeout (default: PROKUBE_TIMEOUT env var).

        Returns:
            A SandboxV2 instance (call ``wait_until_ready()`` before use).
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
        client = SandboxV2Client(config)
        try:
            info = client.create(
                image=image,
                name=name,
                runtime_class=runtime_class,
                vcpus=vcpus,
                mem_mib=mem_mib,
                egress=egress,
                terminal=terminal,
                volumes=volumes,
                volume_mounts=volume_mounts,
                image_pull_secrets=image_pull_secrets,
                workspace_size=workspace_size,
                target_node=target_node,
                operating_mode=operating_mode,
                manifest=manifest,
            )
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            namespace=info.namespace,
            client=client,
            status=info.status,
            image=info.image or image,
            runtime_class=info.runtime_class or runtime_class,
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
        """Connect to an existing Firecracker sandbox by name."""
        config = cls._build_config(
            api_url=api_url,
            namespace=namespace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            info = client.get(name)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            namespace=info.namespace,
            client=client,
            status=info.status,
            image=info.image,
            runtime_class=info.runtime_class,
        )

    # Alias: SandboxV2.connect() is the same as SandboxV2.get()
    connect = get

    @classmethod
    def list(
        cls,
        *,
        phase: str | None = None,
        namespace: str | None = None,
        api_url: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> list[Self]:
        """List Firecracker sandboxes in the namespace."""
        config = cls._build_config(
            api_url=api_url,
            namespace=namespace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            infos = client.list()
        except Exception:
            client.close()
            raise
        client.close()

        if phase is not None:
            infos = [i for i in infos if i.status.value == phase]
        if not infos:
            return []

        sandboxes: list[Self] = []
        try:
            for info in infos:
                sandboxes.append(
                    cls(
                        name=info.name,
                        namespace=info.namespace,
                        client=SandboxV2Client(config, check_version=False),
                        status=info.status,
                        image=info.image,
                        runtime_class=info.runtime_class,
                    )
                )
        except Exception:
            for sbx in sandboxes:
                sbx._client.close()
            raise
        return sandboxes

    @classmethod
    def from_pool(
        cls,
        pool: str,
        *,
        namespace: str | None = None,
        api_url: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Claim a ready member from a warm pool (fast resume, not cold boot).

        Claims a pre-hibernated member from a
        :class:`~prokube.sandboxv2.pool.SandboxV2Pool` and returns a SandboxV2
        bound to it. A claim is a fast VM resume (~1.4s), so unlike
        :meth:`create` the returned sandbox is (or is quickly becoming) Running
        — call :meth:`wait_until_ready` to be sure before use.

        Args:
            pool: Name of the warm pool to claim from.
            namespace: Kubernetes namespace (maps to v1's ``workspace``).
            api_url: API URL (default: PROKUBE_API_URL env var).
            user_id: User ID (default: PROKUBE_USER_ID env var).
            api_key: API key (default: PROKUBE_API_KEY env var).
            timeout: Request timeout (default: PROKUBE_TIMEOUT env var).

        Returns:
            A SandboxV2 instance bound to the claimed member.

        Raises:
            SandboxError: If the pool has no ready member to claim (HTTP 409).

        Example:
            >>> sbx = SandboxV2.from_pool("python-pool")
            >>> sbx.wait_until_ready()
            >>> sbx.run_code("print('hello')")
        """
        config = cls._build_config(
            api_url=api_url,
            namespace=namespace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            info = client.claim(pool)
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            namespace=info.namespace,
            client=client,
            status=info.status,
            image=info.image,
            runtime_class=info.runtime_class,
        )

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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            self.kill()
        except Exception:
            if exc_type is not None:
                return False
            raise
        return False

    def __repr__(self) -> str:
        return (
            f"SandboxV2(name={self._name!r}, "
            f"namespace={self._namespace!r}, "
            f"runtime_class={self._runtime_class!r}, "
            f"status={self._status.value!r})"
        )
