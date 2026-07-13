"""Main SandboxV2 class for Firecracker (Sandbox v2) microVMs.

Mirrors the v1 :class:`prokube.sandbox.Sandbox` public surface, adapted to the
v2 backend: every sandbox runs on ``fc-pod`` (the only runtime; there is no
runtime choice), creation takes v2 knobs (resources, egress, env), and
sandboxes are addressed by the same ``workspace`` param as v1. There is no warm
pool — instead a RUNNING sandbox can be captured into a reusable
FirecrackerTemplate (:meth:`SandboxV2.make_template`) and a later sandbox can
resume-clone from it for a fast start (:meth:`SandboxV2.from_template`).

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
from prokube.common.exceptions import (
    NotFoundError,
    SandboxError,
    SandboxTimeoutError,
)
from prokube.sandbox.code import CodeRunner
from prokube.sandbox.commands import CommandRunner
from prokube.sandbox.files import FileManager
from prokube.sandbox.models import CodeResult
from prokube.sandboxv2.client import SandboxV2Client
from prokube.sandboxv2.models import (
    DNSConfig,
    Lifecycle,
    Probe,
    SandboxV2Condition,
    SandboxV2Info,
    SandboxV2Status,
    Template,
)


class SandboxV2:
    """A Firecracker microVM sandbox for executing code and commands.

    Example:
        >>> sbx = SandboxV2.create(
        ...     image="pk-sandbox-base",
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
        workspace: str,
        client: SandboxV2Client,
        status: SandboxV2Status = SandboxV2Status.PENDING,
        image: str | None = None,
        runtime_class: str | None = None,
        ready: bool = False,
        conditions: list[SandboxV2Condition] | None = None,
    ) -> None:
        """Initialize a SandboxV2 instance.

        Note: use :meth:`create` or :meth:`get` instead of the constructor.
        """
        self._name = name
        self._workspace = workspace
        self._client = client
        self._status = status
        self._image = image
        self._runtime_class = runtime_class
        self._ready = ready
        self._conditions: list[SandboxV2Condition] = conditions or []
        self._killed = False

        self._commands = CommandRunner(client, name, self._check_not_killed)
        self._files = FileManager(client, name, self._check_not_killed)
        self._code = CodeRunner(client, name, self._check_not_killed)

    def _check_not_killed(self) -> None:
        if self._killed:
            raise SandboxError(
                f"Sandbox {self._name} has been killed and cannot be used anymore"
            )

    def _apply_info(self, info: SandboxV2Info) -> None:
        """Fold a fresh :class:`SandboxV2Info` into the cached local state."""
        self._status = info.status
        self._ready = info.ready
        self._conditions = info.conditions
        if info.runtime_class is not None:
            self._runtime_class = info.runtime_class
        if info.image is not None:
            self._image = info.image

    @property
    def name(self) -> str:
        """The sandbox name."""
        return self._name

    @property
    def workspace(self) -> str:
        """The workspace (Kubernetes namespace)."""
        return self._workspace

    @property
    def namespace(self) -> str:
        """Deprecated alias for :attr:`workspace` (v1 uses ``workspace``)."""
        return self._workspace

    @property
    def runtime_class(self) -> str | None:
        """The runtime class reported by the backend (read-only; always
        ``fc-pod`` — there is no runtime choice)."""
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
    def ready(self) -> bool:
        """Whether the sandbox is fully ready (the ``Ready`` condition is True),
        refreshed from the API.

        ``phase == 'Running'`` is NOT sufficient — it now means only that the VM
        process started; ``Ready`` additionally requires the guest image to have
        passed its (optional) startupProbe.
        """
        self.refresh()
        return self._ready

    @property
    def conditions(self) -> list[SandboxV2Condition]:
        """The sandbox's Pod-shaped status conditions (VMStarted / Ready),
        refreshed from the API."""
        self.refresh()
        return self._conditions

    def _condition_time(self, cond_type: str) -> str | None:
        """The ``lastTransitionTime`` of a cached condition, if present."""
        for c in self._conditions:
            if c.type == cond_type:
                return c.last_transition_time
        return None

    @property
    def vm_started_at(self) -> str | None:
        """RFC3339 stamp when the VM process started (VMStarted → True), from the
        last-known conditions (does not refresh)."""
        return self._condition_time("VMStarted")

    @property
    def ready_at(self) -> str | None:
        """RFC3339 stamp when the sandbox became Ready (Ready → True), from the
        last-known conditions (does not refresh)."""
        return self._condition_time("Ready")

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
        """Execute code in the guest's persistent per-language session (stateful).

        SandboxV2 runs the curated sandbox-agent, which keeps one long-lived
        interpreter per language (python/bash/node) — variables, imports and
        shell state persist across calls and survive pause/resume. (There is no
        Jupyter kernel; that was the old execd image.)"""
        self._check_not_killed()
        return self._code.run(code, language=language, timeout=timeout)

    def reset_session(self) -> None:
        """Restart the guest's per-language interpreter session, clearing its
        state for the next ``run_code`` (maps to the agent's ``context.reset``)."""
        self._check_not_killed()
        self._code.reset_session()

    @property
    def session_id(self) -> str | None:
        """Current guest interpreter session ID, if any."""
        return self._code.session_id

    def pause(self) -> None:
        """Pause the sandbox (native VM snapshot to shared storage).

        Session state (the persistent per-language child on the curated agent
        image) survives the snapshot, so pause does NOT reset the session — a
        subsequent :meth:`resume` restores all prior state. Callers who want a
        clean slate call :meth:`reset_session` explicitly.

        Raises:
            SandboxError: If the sandbox is not in Running state (HTTP 409).
        """
        self._check_not_killed()
        info = self._client.pause(self._name)
        self._apply_info(info)
        # A paused (hibernated) sandbox is never ready; if the backend has not yet
        # published the Paused phase, assume it.
        self._ready = False
        if info.status == SandboxV2Status.UNKNOWN:
            self._status = SandboxV2Status.PAUSED

    def resume(self) -> None:
        """Resume a paused sandbox (native VM restore).

        The restored microVM keeps its pre-pause session state (variables,
        imports, shell/node scope), so resume does NOT reset the session.
        Callers who want a clean slate call :meth:`reset_session` explicitly.

        Raises:
            SandboxError: If the sandbox is not in Paused state (HTTP 409).
        """
        self._check_not_killed()
        self._apply_info(self._client.resume(self._name))

    def wait_until_ready(self, timeout: int = 120) -> None:
        """Block until the sandbox is Ready (the ``Ready`` status condition is
        True — the guest image passed its optional startupProbe).

        ``status.phase == Running`` alone is NO LONGER sufficient: it now flips
        the instant the VM *process* starts, before the guest image has warmed
        up. Readiness gates on the ``Ready`` condition (surfaced as the API's
        flattened ``ready`` boolean). With no startupProbe the image is Ready
        almost immediately after the VM starts.

        A ready microVM resumes in well under a second, so this prefers the
        backend's server-side long-poll readiness endpoint and otherwise falls
        back to a tight local ``get`` poll (~100ms, gently backing off to 1s).

        Args:
            timeout: Maximum seconds to wait (default: 120).

        Raises:
            SandboxTimeoutError: If it does not become Ready in time.
            SandboxError: If it enters the Failed terminal state while waiting.
        """
        self._check_not_killed()
        # Fast path: an already-ready sandbox (e.g. a resumed one whose Ready
        # condition is already True) skips the readiness round-trip entirely.
        if self._ready:
            return
        deadline = time.monotonic() + timeout
        use_long_poll = True
        # Poll cadence: start tight (matches the ~0.4s control-plane lag) and
        # back off so a slow-to-ready sandbox does not hammer the backend for the
        # full timeout window. Also throttles the long-poll path when the server
        # resolves on phase==Running before the Ready condition has flipped.
        poll_interval = 0.1

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if use_long_poll:
                try:
                    # Server long-polls up to its own window; loop here until our
                    # deadline. Cap each call so a stalled connection still
                    # rechecks our timeout periodically.
                    info = self._client.wait_ready(
                        self._name, timeout=min(int(remaining) + 1, 30)
                    )
                except NotFoundError:
                    # Endpoint absent (older backend) or sandbox genuinely
                    # missing: drop to the local poll, which re-raises the real
                    # not-found error if the sandbox truly does not exist.
                    use_long_poll = False
                    continue
                self._apply_info(info)
                if self._ready:
                    return
                if self._status == SandboxV2Status.FAILED:
                    raise SandboxError(
                        f"Sandbox {self._name} entered terminal state "
                        f"{self._status.value!r} while waiting for it to become "
                        f"ready"
                    )
                # The endpoint may resolve on phase==Running while the guest image
                # is still warming (Ready not yet True). Sleep before re-polling so
                # we do not busy-spin waiting on the Ready condition.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))
                # Cap the backoff low (0.25s): an older backend resolves this
                # long-poll on phase==Running (before Ready), so the cap bounds
                # how far past the true Ready moment we can overshoot here.
                poll_interval = min(poll_interval * 1.5, 0.25)
                continue

            self.refresh()
            if self._ready:
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
            poll_interval = min(poll_interval * 1.5, 1.0)

        raise SandboxTimeoutError(
            f"Sandbox {self._name} did not become ready within {timeout}s "
            f"(current phase: {self._status.value!r}, ready={self._ready})"
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

    def make_template(self, name: str) -> str:
        """Capture this RUNNING sandbox into a reusable FirecrackerTemplate.

        The backend captures the microVM ASYNCHRONOUSLY: this call returns as
        soon as the capture request is accepted, not once the template is
        ``Ready``. This sandbox keeps running throughout — capturing does
        not pause or kill it. Poll for readiness via
        :meth:`SandboxV2.list_templates` (``phase == "Ready"``) or
        :meth:`wait_for_template_ready`; use :meth:`from_template` once you
        know it is ready.

        Args:
            name: Name for the new FirecrackerTemplate.

        Returns:
            The template name (echoed back by the backend).

        Raises:
            SandboxError: If this sandbox is not in Running state (HTTP 409).
        """
        self._check_not_killed()
        info = self._client.make_template(self._name, name)
        return info.name

    def refresh(self) -> None:
        """Refresh sandbox information from the API."""
        self._check_not_killed()
        self._apply_info(self._client.get(self._name))

    # -- constructors ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        image: str | None = None,
        *,
        name: str | None = None,
        template: str | None = None,
        resources: dict | None = None,
        vcpus: int | None = None,
        mem_mib: int | None = None,
        overlay_mib: int | None = None,
        egress: bool = False,
        terminal: bool = True,
        env_vars: dict[str, str] | list[dict[str, str]] | None = None,
        secret_refs: list[str] | None = None,
        image_pull_secrets: list[str] | None = None,
        operating_mode: str | None = None,
        startup_probe: Probe | dict | None = None,
        lifecycle: Lifecycle | dict | None = None,
        dns_policy: str | None = None,
        dns_config: DNSConfig | dict | None = None,
        mesh: bool | None = None,
        snapshot_resume_policy: str | None = None,
        manifest: dict | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Create a new Firecracker sandbox.

        Args:
            image: Base OCI image. If None (and ``template`` is also None),
                the backend default (pk-sandbox-base) is used. Mutually
                exclusive with ``template``.
            name: Optional sandbox name (auto-generated if not provided).
            template: Name of an existing FirecrackerTemplate (created via
                :meth:`make_template`, once ``Ready``) to resume-clone from
                instead of an OCI image (``spec.template``).
                Mutually exclusive with ``image``. Prefer
                :meth:`from_template`, which wraps this with resume-oriented
                defaults.
            resources: Optional ``{"vcpus": int, "mem_mib": int, "overlay_mib":
                int}`` shorthand. Explicit ``vcpus`` / ``mem_mib`` /
                ``overlay_mib`` kwargs take precedence.
            vcpus: Guest vCPUs (overrides ``resources['vcpus']``).
            mem_mib: Guest memory in MiB (overrides ``resources['mem_mib']``).
            overlay_mib: Writable-overlay (rootfs scratch) cap in MiB. Sparse, so
                a ceiling not a reservation (overrides ``resources['overlay_mib']``).
                Omitted -> CRD default (512).
            egress: Whether the microVM may reach the cluster/internet
                (default: False — isolated).
            terminal: Inject a ttyd Terminal (:7681) into the guest.
            env_vars: Literal env vars baked into the guest. Accepts a
                ``dict[str,str]`` or a list of ``{"name","value"}`` dicts;
                serializes to CRD ``spec.env``. Not refreshed on pause/resume.
            secret_refs: Names of Secrets (in the sandbox namespace) whose keys
                are injected as env vars; serializes to CRD ``spec.envFrom``.
            image_pull_secrets: Registry pull secret names.
            operating_mode: ``Running`` or ``Hibernated``.
            startup_probe: spec.startupProbe (core/v1 Probe) gating boot
                readiness. A :class:`~prokube.sandboxv2.models.Probe` or a
                CR-shaped dict. Omitted -> backend execd default.
            lifecycle: spec.lifecycle (core/v1 Lifecycle; ``postStart`` warm-up
                hook). A :class:`~prokube.sandboxv2.models.Lifecycle` or a
                CR-shaped dict. Omitted -> backend execd default.
            dns_policy: spec.dnsPolicy (``ClusterFirst`` | ``None`` | ``Default``)
                — how the guest /etc/resolv.conf is written at cold boot.
                Omitted -> executor ClusterFirst default.
            dns_config: spec.dnsConfig (Pod PodDNSConfig — nameservers/searches/
                options merged into the guest resolv.conf). A
                :class:`~prokube.sandboxv2.models.DNSConfig` or a CR-shaped dict.
            mesh: Optional: opt this sandbox into the Istio service mesh
                (spec.mesh).
            snapshot_resume_policy: spec.snapshotResumePolicy (``Strict`` |
                ``AllowStale``) — whether resuming from a template
                requires an exact recipe/base match. Omitted -> executor
                Strict default.
            manifest: Full FirecrackerSandbox object; wins over structured knobs.
            api_url: API URL (default: PROKUBE_API_URL env var).
            workspace: Workspace / Kubernetes namespace (default:
                PROKUBE_WORKSPACE env var).
            user_id: User ID (default: PROKUBE_USER_ID env var).
            api_key: API key (default: PROKUBE_API_KEY env var).
            timeout: Request timeout (default: PROKUBE_TIMEOUT env var).

        Returns:
            A SandboxV2 instance (call ``wait_until_ready()`` before use).
        """
        if resources:
            vcpus = vcpus if vcpus is not None else resources.get("vcpus")
            mem_mib = mem_mib if mem_mib is not None else resources.get("mem_mib")
            overlay_mib = (
                overlay_mib if overlay_mib is not None
                else resources.get("overlay_mib")
            )

        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            info = client.create(
                image=image,
                name=name,
                template=template,
                vcpus=vcpus,
                mem_mib=mem_mib,
                overlay_mib=overlay_mib,
                egress=egress,
                terminal=terminal,
                env_vars=env_vars,
                secret_refs=secret_refs,
                image_pull_secrets=image_pull_secrets,
                operating_mode=operating_mode,
                startup_probe=startup_probe,
                lifecycle=lifecycle,
                dns_policy=dns_policy,
                dns_config=dns_config,
                mesh=mesh,
                snapshot_resume_policy=snapshot_resume_policy,
                manifest=manifest,
            )
        except Exception:
            client.close()
            raise

        return cls(
            name=info.name,
            workspace=info.workspace,
            client=client,
            status=info.status,
            image=info.image or image,
            runtime_class=info.runtime_class,
            ready=info.ready,
            conditions=info.conditions,
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
        """Connect to an existing Firecracker sandbox by name."""
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
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
            workspace=info.workspace,
            client=client,
            status=info.status,
            image=info.image,
            runtime_class=info.runtime_class,
            ready=info.ready,
            conditions=info.conditions,
        )

    # Alias: SandboxV2.connect() is the same as SandboxV2.get()
    connect = get

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
        """List Firecracker sandboxes in the workspace."""
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
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
                        workspace=info.workspace,
                        client=SandboxV2Client(config, check_version=False),
                        status=info.status,
                        image=info.image,
                        runtime_class=info.runtime_class,
                        ready=info.ready,
                        conditions=info.conditions,
                    )
                )
        except Exception:
            for sbx in sandboxes:
                sbx._client.close()
            raise
        return sandboxes

    @classmethod
    def list_templates(
        cls,
        *,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> list[Template]:
        """List FirecrackerTemplates in the workspace.

        Best-effort: returns an empty list (never raises) when template
        listing is unavailable backend-side (CRD missing, RBAC).
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
        )
        client = SandboxV2Client(config)
        try:
            templates = client.templates()
        finally:
            client.close()
        return templates

    @classmethod
    def wait_for_template_ready(
        cls,
        image: str,
        *,
        timeout: int = 120,
        poll_interval: float = 1.0,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        request_timeout: int | None = None,
    ) -> Template:
        """Block until the named FirecrackerTemplate reaches ``Ready``.

        Polls :meth:`list_templates` (there is no server-side long-poll for
        FirecrackerTemplate readiness, unlike :meth:`wait_until_ready`).

        Args:
            image: Name of the FirecrackerTemplate to wait for.
            timeout: Maximum seconds to wait (default: 120).
            poll_interval: Seconds between polls (default: 1.0).
            api_url: API URL (default: PROKUBE_API_URL env var).
            workspace: Workspace / Kubernetes namespace (default:
                PROKUBE_WORKSPACE env var).
            user_id: User ID (default: PROKUBE_USER_ID env var).
            api_key: API key (default: PROKUBE_API_KEY env var).
            request_timeout: Per-request timeout (default: PROKUBE_TIMEOUT env
                var).

        Returns:
            The ``Ready`` :class:`~prokube.sandboxv2.models.Template`.

        Raises:
            SandboxTimeoutError: If it does not become Ready in time (or is
                never observed).
            SandboxError: If it enters the ``Failed`` terminal state while
                waiting.
        """
        config = cls._build_config(
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=request_timeout,
        )
        client = SandboxV2Client(config)
        last: Template | None = None
        try:
            deadline = time.monotonic() + timeout
            while True:
                for img in client.templates():
                    if img.name != image:
                        continue
                    last = img
                    if img.phase == "Ready":
                        return img
                    if img.phase == "Failed":
                        detail = f": {img.message}" if img.message else ""
                        raise SandboxError(
                            f"Template {image!r} entered terminal state "
                            f"'Failed'{detail}"
                        )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval, remaining))
        finally:
            client.close()

        current = f" (current phase: {last.phase!r})" if last else " (not found)"
        raise SandboxTimeoutError(
            f"Template {image!r} did not become Ready within {timeout}s{current}"
        )

    @classmethod
    def from_template(
        cls,
        image: str,
        *,
        name: str | None = None,
        resources: dict | None = None,
        vcpus: int | None = None,
        mem_mib: int | None = None,
        overlay_mib: int | None = None,
        egress: bool = False,
        terminal: bool = True,
        env_vars: dict[str, str] | list[dict[str, str]] | None = None,
        secret_refs: list[str] | None = None,
        mesh: bool | None = None,
        snapshot_resume_policy: str | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> Self:
        """Launch a new sandbox by resume-cloning a FirecrackerTemplate.

        ``image`` must be the name of a FirecrackerTemplate created by
        :meth:`make_template` (once its ``status.phase`` reaches ``Ready`` — see
        :meth:`wait_for_template_ready`) — NOT an OCI ref. Sets ``template``
        on the create request, which the backend maps onto
        ``spec.template`` (a structured knob, mutually exclusive
        with ``image``); no ``manifest`` escape hatch needed.

        Args:
            image: Name of the FirecrackerTemplate to resume-clone from.
            name: Optional sandbox name (auto-generated if not provided).
            resources: ``{"vcpus": int, "mem_mib": int, "overlay_mib": int}``
                shorthand.
            vcpus: Guest vCPUs (overrides ``resources['vcpus']``; default 2).
            mem_mib: Guest memory in MiB (overrides ``resources['mem_mib']``;
                default 2048).
            overlay_mib: Writable-overlay cap in MiB (sparse ceiling; overrides
                ``resources['overlay_mib']``). Omitted -> CRD default (512).
            egress: Whether the microVM may reach the cluster/internet
                (default: False — isolated).
            terminal: Inject a ttyd Terminal (:7681) into the guest.
            env_vars: Literal env vars baked into the guest. Accepts a
                ``dict[str,str]`` or a list of ``{"name","value"}`` dicts.
            secret_refs: Names of Secrets whose keys are injected as env vars.
            mesh: Optional: opt this sandbox into the Istio service mesh.
            snapshot_resume_policy: ``Strict`` | ``AllowStale`` — whether the
                resume requires an exact recipe/base match with the template.
            api_url: API URL (default: PROKUBE_API_URL env var).
            workspace: Workspace / Kubernetes namespace (default:
                PROKUBE_WORKSPACE env var).
            user_id: User ID (default: PROKUBE_USER_ID env var).
            api_key: API key (default: PROKUBE_API_KEY env var).
            timeout: Request timeout (default: PROKUBE_TIMEOUT env var).

        Returns:
            A SandboxV2 instance (call ``wait_until_ready()`` before use).

        Example:
            >>> sbx = SandboxV2.from_template("my-warm-python")
            >>> sbx.wait_until_ready()
            >>> sbx.run_code("print('hello')")
        """
        if resources:
            vcpus = vcpus if vcpus is not None else resources.get("vcpus")
            mem_mib = mem_mib if mem_mib is not None else resources.get("mem_mib")
            overlay_mib = (
                overlay_mib if overlay_mib is not None
                else resources.get("overlay_mib")
            )

        return cls.create(
            name=name,
            template=image,
            vcpus=vcpus if vcpus is not None else 2,
            mem_mib=mem_mib if mem_mib is not None else 2048,
            overlay_mib=overlay_mib,
            egress=egress,
            terminal=terminal,
            env_vars=env_vars,
            secret_refs=secret_refs,
            operating_mode="Running",
            mesh=mesh,
            snapshot_resume_policy=snapshot_resume_policy,
            api_url=api_url,
            workspace=workspace,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
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
            f"workspace={self._workspace!r}, "
            f"runtime_class={self._runtime_class!r}, "
            f"status={self._status.value!r})"
        )
