"""Sandboxes on Agent Substrate.

A sandbox is an Actor named ``sbx-<name>`` in the Atespace of a workspace
(Atespace name = workspace namespace). Lifecycle calls go to the ate API
(``ateapi.Control``); commands and files go to the env guest API through the
upstream ``ate_env`` client. Every call carries the caller's token, which
Agent Gateway passes through to Substrate. Results are the upstream types
(``Actor`` protos, ``ate_env.ShellResult``) and errors the ``ate_env`` ones.

In a workspace pod with a workload token (prokube/prokube-neo#100) and no
platform URL, the client calls Agent Gateway's in-cluster Service as the pod
and defaults to the pod's workspace.
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
from collections.abc import Awaitable, Coroutine, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

import grpc
import grpc.aio
import httpx
from ate_env import Client as EnvClient
from ate_env import Env, RpcError, ShellResult, map_rpc_error

from prokube import auth, endpoints
from prokube.auth import TokenProvider
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox._ateapi.ateapi_pb2_grpc import ControlStub
from prokube.sandbox.presets import PresetError, Presets

SANDBOX_PREFIX = "sbx-"
# Every Actor's EgressPolicy is a singleton with this name, as in pkui.
EGRESS_POLICY_NAME = "default"
# The env guest of pk-sandbox-coding answers here once it serves; templates
# whose guest container has this wakeup probe support exec, files and SSH.
ENV_GUEST_PATH = "/readyz"
ENV_GUEST_PORT = 80

# Namespace of the pod, which is its workspace.
NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

Actor = ateapi.Actor
T = TypeVar("T")


def actor_name(name: str) -> str:
    """Return the Actor name of a sandbox; names already starting with sbx- are kept."""
    return name if name.startswith(SANDBOX_PREFIX) else SANDBOX_PREFIX + name


class AsyncSandboxClient:
    """Create and use the sandboxes of one workspace.

    ``url`` (``https://<domain>``) defaults to ``$PROKUBE_URL`` and
    ``workspace`` to ``$PROKUBE_WORKSPACE``, both then to what ``pk login``
    saved. ``token_provider`` defaults to the
    stored login for ``url`` (see ``prokube.auth.login``), then to the pod's
    workload token. In a workspace pod with a workload token, all three can
    be left out: the workspace defaults to the pod's namespace and, without a
    URL, the client uses Agent Gateway's in-cluster Service (``url`` is then
    None). Create the client inside the event loop that uses it.
    """

    def __init__(
        self,
        url: str | None = None,
        workspace: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
    ) -> None:
        saved = auth.load_config()
        url = url or os.environ.get("PROKUBE_URL") or saved.get("url")
        workspace = (
            workspace or os.environ.get("PROKUBE_WORKSPACE") or saved.get("workspace")
        )
        in_pod = auth.workload_token_file() is not None
        if in_pod:
            workspace = workspace or _pod_namespace()
        if not (url or in_pod) or not workspace:
            raise ValueError(
                "Pass url and workspace, set PROKUBE_URL and PROKUBE_WORKSPACE, "
                "or run in a workspace pod with sandbox access."
            )
        self.url = endpoints.normalize_url(url) if url else None
        self.workspace = workspace
        if token_provider is None:
            token_provider = auth.default_token_provider(self.url)
        self._token_provider = token_provider
        if self.url:
            credentials = grpc.composite_channel_credentials(
                grpc.ssl_channel_credentials(),
                grpc.metadata_call_credentials(_BearerToken(token_provider)),
            )
            self._ate_channel = grpc.aio.secure_channel(
                endpoints.ate_api_target(self.url), credentials
            )
            self._env_channel = grpc.aio.secure_channel(
                endpoints.env_target(self.url), credentials
            )
        else:
            # gRPC sends call credentials only over TLS, so the token goes in
            # by interceptor, one per call type: grpc.aio registers an
            # interceptor for only the first type it implements.
            interceptors: list[grpc.aio.ClientInterceptor] = [
                _UnaryUnaryBearerToken(token_provider),
                _UnaryStreamBearerToken(token_provider),
                _StreamUnaryBearerToken(token_provider),
                _StreamStreamBearerToken(token_provider),
            ]
            self._ate_channel = grpc.aio.insecure_channel(
                endpoints.IN_CLUSTER_GATEWAY, interceptors=interceptors
            )
            self._env_channel = grpc.aio.insecure_channel(
                endpoints.IN_CLUSTER_GATEWAY, interceptors=interceptors
            )
        self._control = ControlStub(self._ate_channel)
        self._env = EnvClient(channel=self._env_channel)

    async def create(self, name: str, *, preset: str | None = None) -> Actor:
        """Create a sandbox from a preset (default: the platform's default one).

        The preset's golden snapshot makes this fast, and the sandbox gets the
        preset's network rules. It starts SUSPENDED and wakes on its first
        command or on resume().
        """
        chosen = (await self.presets()).get(preset)
        actor = ateapi.Actor(
            metadata=ateapi.ResourceMetadata(
                atespace=self.workspace, name=actor_name(name)
            ),
            actor_template=chosen.ref,
        )
        request = ateapi.CreateActorRequest(actor=actor)
        created = await _call(self._control.CreateActor(request))
        policy = ateapi.EgressPolicy(
            metadata=ateapi.ResourceMetadata(
                atespace=self.workspace, name=EGRESS_POLICY_NAME
            ),
            rules=chosen.network.rules,
        )
        request = ateapi.CreateActorEgressPolicyRequest(
            actor=self._ref(name), egress_policy=policy
        )
        try:
            await _call(self._control.CreateActorEgressPolicy(request))
        except RpcError as error:  # it has one already; keep that
            if error.code != grpc.StatusCode.ALREADY_EXISTS:
                raise
        return created

    async def resize(self, name: str, *, preset: str) -> Actor:
        """Switch a sandbox to another preset, e.g. one with more CPU and memory.

        The preset must have the sandbox's image and snapshot fidelity. A
        running sandbox is suspended first and resumed afterwards: /workspace
        is kept, running processes restart. A suspended sandbox stays
        suspended. Network rules and credentials stay.
        """
        actor = await self.get(name)
        target = (await self.presets()).get(preset).ref
        if target == actor.actor_template:
            return actor
        current = await self._template(actor.actor_template)
        new = await self._template(target)
        for what, have, want in (
            ("image", current.containers[0].image, new.containers[0].image),
            (
                "snapshot fidelity",
                current.snapshot_config.preferred_fidelity,
                new.snapshot_config.preferred_fidelity,
            ),
        ):
            if have != want:
                raise PresetError(
                    f"Preset {preset!r} has another {what} than sandbox {name!r}. "
                    "Create a new sandbox from it instead."
                )
        was_running = actor.status.state == ateapi.ACTOR_STATE_RUNNING
        # A no-op for a suspended sandbox; also uploads a paused one's snapshot.
        actor = await self.suspend(name)
        actor.actor_template.CopyFrom(target)
        actor.ClearField("status")
        request = ateapi.UpdateActorRequest(actor=actor)
        actor = await _call(self._control.UpdateActor(request))
        return await self.resume(name) if was_running else actor

    async def presets(self) -> Presets:
        """The presets the platform offers and its default preset."""
        url = endpoints.presets_url(self.url)
        token = await asyncio.to_thread(self._token_provider)
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(url, headers={"authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise PresetError(
                f"Cannot read the sandbox presets from {url}: HTTP {response.status_code}"
            )
        return Presets.from_json(response.json())

    async def has_env_guest(self, name: str) -> bool:
        """Whether the sandbox's image is based on pk-sandbox-coding.

        Only then do run, read_file, write_file, env(), the terminal and SSH work.
        """
        actor = await self.get(name)
        template = await self._template(actor.actor_template)
        return any(
            container.name == "guest"
            and container.wakeup_probe.http_get.path == ENV_GUEST_PATH
            and container.wakeup_probe.http_get.port == ENV_GUEST_PORT
            for container in template.containers
        )

    async def list(self) -> list[Actor]:
        """Return all sandboxes of the workspace."""
        sandboxes: list[Actor] = []
        page_token = ""
        while True:
            request = ateapi.ListActorsRequest(
                atespace=self.workspace,
                options=ateapi.ListOptions(page_token=page_token),
            )
            response = await _call(self._control.ListActors(request))
            sandboxes += [
                actor
                for actor in response.actors
                if actor.metadata.name.startswith(SANDBOX_PREFIX)
            ]
            page_token = response.next_page_token
            if not page_token:
                return sandboxes

    async def get(self, name: str) -> Actor:
        request = ateapi.GetActorRequest(actor=self._ref(name))
        return await _call(self._control.GetActor(request))

    async def delete(self, name: str) -> Actor:
        """Delete a sandbox in any state, including a running one."""
        request = ateapi.DeleteActorRequest(actor=self._ref(name), any_state=True)
        return await _call(self._control.DeleteActor(request))

    async def suspend(self, name: str) -> Actor:
        """Snapshot a running sandbox and free its worker."""
        request = ateapi.SuspendActorRequest(actor=self._ref(name))
        return (await _call(self._control.SuspendActor(request))).actor

    async def resume(self, name: str) -> Actor:
        """Start a suspended sandbox from its latest snapshot."""
        request = ateapi.ResumeActorRequest(actor=self._ref(name))
        return (await _call(self._control.ResumeActor(request))).actor

    def env(self, name: str) -> Env:
        """Return the upstream env handle for processes and files.

        Use this client, not the handle, for lifecycle calls.
        """
        return self._env.env(actor_name(name), atespace=self.workspace)

    async def run(
        self,
        name: str,
        command: str,
        *,
        stdin: bytes | str | None = None,
        cwd: str = "",
        env: Mapping[str, str] | None = None,
        timeout: float | timedelta | None = None,
    ) -> ShellResult:
        """Run ``sh -c command`` in the sandbox and wait for it to exit."""
        return await self.env(name).shell(
            command, stdin=stdin, cwd=cwd, env=env, timeout=timeout
        )

    async def read_file(self, name: str, path: str) -> bytes:
        """Read a file; the env file API only allows paths under /workspace."""
        return await self.env(name).read_file_bytes(path)

    async def write_file(
        self, name: str, path: str, data: bytes | str, *, mode: int = 0o644
    ) -> int:
        """Write a file, replacing it; returns the number of bytes written.

        The env file API only allows paths under /workspace.
        """
        if isinstance(data, str):
            data = data.encode()
        return await self.env(name).write_file(path, data, mode=mode)

    async def close(self) -> None:
        await self._ate_channel.close()
        await self._env_channel.close()

    async def __aenter__(self) -> AsyncSandboxClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _ref(self, name: str) -> ateapi.ObjectRef:
        return ateapi.ObjectRef(atespace=self.workspace, name=actor_name(name))

    async def _template(self, ref: ateapi.ObjectRef) -> ateapi.ActorTemplate:
        request = ateapi.GetActorTemplateRequest(actor_template=ref)
        return await _call(self._control.GetActorTemplate(request))


class SandboxClient:
    """Blocking version of AsyncSandboxClient; also works inside notebooks.

    It runs an AsyncSandboxClient on its own event loop in a background thread.
    """

    def __init__(
        self,
        url: str | None = None,
        workspace: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        async def create_client() -> AsyncSandboxClient:
            return AsyncSandboxClient(url, workspace, token_provider=token_provider)

        try:
            self._client = self._run(create_client())
        except BaseException:
            self._stop_loop()
            raise
        self.url = self._client.url
        self.workspace = self._client.workspace

    def create(self, name: str, *, preset: str | None = None) -> Actor:
        """Create a sandbox from a preset (see AsyncSandboxClient)."""
        return self._run(self._client.create(name, preset=preset))

    def resize(self, name: str, *, preset: str) -> Actor:
        """Switch a sandbox to another preset (see AsyncSandboxClient)."""
        return self._run(self._client.resize(name, preset=preset))

    def presets(self) -> Presets:
        return self._run(self._client.presets())

    def has_env_guest(self, name: str) -> bool:
        return self._run(self._client.has_env_guest(name))

    def list(self) -> list[Actor]:
        return self._run(self._client.list())

    def get(self, name: str) -> Actor:
        return self._run(self._client.get(name))

    def delete(self, name: str) -> Actor:
        return self._run(self._client.delete(name))

    def suspend(self, name: str) -> Actor:
        return self._run(self._client.suspend(name))

    def resume(self, name: str) -> Actor:
        return self._run(self._client.resume(name))

    def run(
        self,
        name: str,
        command: str,
        *,
        stdin: bytes | str | None = None,
        cwd: str = "",
        env: Mapping[str, str] | None = None,
        timeout: float | timedelta | None = None,
    ) -> ShellResult:
        return self._run(
            self._client.run(
                name, command, stdin=stdin, cwd=cwd, env=env, timeout=timeout
            )
        )

    def read_file(self, name: str, path: str) -> bytes:
        return self._run(self._client.read_file(name, path))

    def write_file(
        self, name: str, path: str, data: bytes | str, *, mode: int = 0o644
    ) -> int:
        return self._run(self._client.write_file(name, path, data, mode=mode))

    def close(self) -> None:
        self._run(self._client.close())
        self._stop_loop()

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


class _BearerToken(grpc.AuthMetadataPlugin):
    """Adds ``authorization: Bearer <token>``, asking the provider on every call."""

    def __init__(self, token_provider: TokenProvider) -> None:
        self._token_provider = token_provider

    def __call__(
        self,
        context: grpc.AuthMetadataContext,
        callback: grpc.AuthMetadataPluginCallback,
    ) -> None:
        try:
            callback((("authorization", f"Bearer {self._token_provider()}"),), None)
        except Exception as error:  # e.g. LoginRequired; fails the call with it
            callback((), error)


class _CallDetails(
    collections.namedtuple(
        "_CallDetails",
        ["method", "timeout", "metadata", "credentials", "wait_for_ready"],
    ),
    grpc.aio.ClientCallDetails,
):
    pass


class _BearerTokenInterceptor:
    """Adds ``authorization: Bearer <token>`` on plaintext channels.

    Like the upstream ``ate_env`` client's, but asks the provider on every call.
    """

    def __init__(self, token_provider: TokenProvider) -> None:
        self._token_provider = token_provider

    def _with_token(self, details: grpc.aio.ClientCallDetails) -> _CallDetails:
        metadata = list(details.metadata or [])
        metadata.append(("authorization", f"Bearer {self._token_provider()}"))
        return _CallDetails(
            details.method,
            details.timeout,
            metadata,
            details.credentials,
            details.wait_for_ready,
        )


class _UnaryUnaryBearerToken(
    _BearerTokenInterceptor, grpc.aio.UnaryUnaryClientInterceptor
):
    async def intercept_unary_unary(self, continuation, client_call_details, request):
        return await continuation(self._with_token(client_call_details), request)


class _UnaryStreamBearerToken(
    _BearerTokenInterceptor, grpc.aio.UnaryStreamClientInterceptor
):
    async def intercept_unary_stream(self, continuation, client_call_details, request):
        return await continuation(self._with_token(client_call_details), request)


class _StreamUnaryBearerToken(
    _BearerTokenInterceptor, grpc.aio.StreamUnaryClientInterceptor
):
    async def intercept_stream_unary(
        self, continuation, client_call_details, request_iterator
    ):
        return await continuation(
            self._with_token(client_call_details), request_iterator
        )


class _StreamStreamBearerToken(
    _BearerTokenInterceptor, grpc.aio.StreamStreamClientInterceptor
):
    async def intercept_stream_stream(
        self, continuation, client_call_details, request_iterator
    ):
        return await continuation(
            self._with_token(client_call_details), request_iterator
        )


def _pod_namespace() -> str | None:
    path = Path(NAMESPACE_FILE)
    return path.read_text().strip() if path.is_file() else None


async def _call(awaitable: Awaitable[T]) -> T:
    """Await an ate API call and raise the same errors as the env client."""
    try:
        return await awaitable
    except grpc.RpcError as error:
        raise map_rpc_error(error) from error
