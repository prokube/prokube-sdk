"""Sandboxes on Agent Substrate.

A sandbox is an Actor named ``sbx-<name>`` in the Atespace of a workspace
(Atespace name = workspace namespace). Lifecycle calls go to the ate API
(``ateapi.Control``); commands and files go to the env guest API through the
upstream ``ate_env`` client. Every call carries the caller's token, which
Agent Gateway passes through to Substrate. Results are the upstream types
(``Actor`` protos, ``ate_env.ShellResult``) and errors the ``ate_env`` ones.

Without a platform URL, or with ``endpoint``, the client calls Agent
Gateway's in-cluster Service. In a workspace pod with a workload token
(prokube/prokube-neo#100), it acts as the pod and defaults to the pod's
workspace.
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
from collections.abc import (
    AsyncIterable,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

import grpc
import grpc.aio
import httpx
from ate_env import Client as EnvClient
from ate_env import Env, RpcError, ShellResult, map_rpc_error
from google.protobuf import json_format

from prokube import auth, endpoints
from prokube.auth import TokenProvider
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox._ateapi.ateapi_pb2_grpc import ControlStub
from prokube.sandbox.presets import PresetError, Presets

SANDBOX_PREFIX = "sbx-"
# Every Actor's EgressPolicy is a singleton with this name, as in pkui.
EGRESS_POLICY_NAME = "default"
# Credential URI of a key in a Secret of a workspace; the platform's credential
# provider resolves it for that workspace's sandboxes (prokube/prokube-neo#116).
SECRET_URI = "ate-secret://k8s.io/default/{workspace}/{secret}/{key}"
# The env guest of pk-sandbox-coding answers here once it serves; templates
# whose guest container has this wakeup probe support exec, files and SSH.
ENV_GUEST_PATH = "/readyz"
ENV_GUEST_PORT = 80

# Namespace of the pod, which is its workspace.
NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

Actor = ateapi.Actor
ActorAssignment = ateapi.ActorAssignment
Worker = ateapi.Worker
# What write_file() takes; an (async) iterable of chunks is streamed.
FileData = bytes | str | Iterable[bytes] | AsyncIterable[bytes]
T = TypeVar("T")


def actor_name(name: str) -> str:
    """Return the Actor name of a sandbox; names already starting with sbx- are kept."""
    return name if name.startswith(SANDBOX_PREFIX) else SANDBOX_PREFIX + name


class AsyncSandboxClient:
    """Create and use the sandboxes of a workspace.

    ``url`` (``https://<domain>``) defaults to ``$PROKUBE_URL`` and
    ``workspace`` to ``$PROKUBE_WORKSPACE``, both then to what ``pk login``
    saved. ``token_provider`` defaults to the stored login for ``url`` (see
    ``prokube.auth.login``), then to the pod's workload token.

    Without a URL, or with ``endpoint``, calls go in plaintext to Agent
    Gateway's in-cluster Service at ``endpoint`` (``host:port``, default
    ``endpoints.IN_CLUSTER_GATEWAY``). In a workspace pod with a workload
    token, the workspace defaults to the pod's namespace. Without a
    workspace, only presets(), workers(), worker_actors() and
    list_actors(all_workspaces=True) work.

    The client connects on its first call; close() releases it. Create it
    inside the event loop that uses it.
    """

    def __init__(
        self,
        url: str | None = None,
        workspace: str | None = None,
        *,
        endpoint: str | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        saved = auth.load_config()
        url = url or os.environ.get("PROKUBE_URL") or saved.get("url")
        workspace = (
            workspace or os.environ.get("PROKUBE_WORKSPACE") or saved.get("workspace")
        )
        if not workspace and auth.workload_token_file():
            workspace = _pod_namespace()
        self.url = endpoints.normalize_url(url) if url else None
        self.workspace = workspace
        if token_provider is None:
            token_provider = auth.default_token_provider(self.url)
        self._token_provider = token_provider
        if self.url and not endpoint:
            self.endpoint = None
            credentials = grpc.composite_channel_credentials(
                grpc.ssl_channel_credentials(),
                grpc.metadata_call_credentials(_BearerToken(token_provider)),
            )
            self._channel = grpc.aio.secure_channel(
                endpoints.grpc_target(self.url), credentials
            )
            self._presets_url = endpoints.presets_url(self.url)
        else:
            self.endpoint = endpoint or endpoints.IN_CLUSTER_GATEWAY
            # gRPC sends call credentials only over TLS, so the token goes in
            # by interceptor, one per call type: grpc.aio registers an
            # interceptor for only the first type it implements.
            interceptors: list[grpc.aio.ClientInterceptor] = [
                _UnaryUnaryBearerToken(token_provider),
                _UnaryStreamBearerToken(token_provider),
                _StreamUnaryBearerToken(token_provider),
                _StreamStreamBearerToken(token_provider),
            ]
            self._channel = grpc.aio.insecure_channel(
                self.endpoint, interceptors=interceptors
            )
            self._presets_url = endpoints.presets_url(f"http://{self.endpoint}")
        self._control = ControlStub(self._channel)
        self._env = EnvClient(channel=self._channel)

    async def create(
        self,
        name: str,
        *,
        preset: str | None = None,
        network: Sequence[Mapping[str, Any]] | None = None,
    ) -> Actor:
        """Create a sandbox from a preset (default: the platform's default one).

        The preset's golden snapshot makes this fast. The sandbox gets the
        preset's network rules, or ``network`` instead (rules as for
        set_network()). It starts SUSPENDED and wakes on its first command or
        on resume().
        """
        ref = self._ref(name)
        chosen = (await self.presets()).get(preset)
        rules = chosen.network.rules if network is None else _parse_rules(network)
        actor = ateapi.Actor(
            metadata=ateapi.ResourceMetadata(atespace=ref.atespace, name=ref.name),
            actor_template=chosen.ref,
        )
        request = ateapi.CreateActorRequest(actor=actor)
        created = await _call(self._control.CreateActor(request))
        policy = ateapi.EgressPolicy(
            metadata=ateapi.ResourceMetadata(
                atespace=ref.atespace, name=EGRESS_POLICY_NAME
            ),
            rules=rules,
        )
        request = ateapi.CreateActorEgressPolicyRequest(actor=ref, egress_policy=policy)
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

    async def network(self, name: str) -> list[dict[str, Any]]:
        """The sandbox's egress rules, as JSON like the presets' network rules."""
        return _rules(await self._egress_policy(name))

    async def set_network(
        self, name: str, rules: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace the sandbox's egress rules; they apply right away.

        A rule is the JSON of an ``ateapi.EgressRule``; passthrough() and
        inject() build the common ones.
        """
        policy = await self._egress_policy(name)
        del policy.rules[:]
        policy.rules.extend(_parse_rules(rules))
        request = ateapi.UpdateActorEgressPolicyRequest(
            actor=self._ref(name), egress_policy=policy
        )
        return _rules(await _call(self._control.UpdateActorEgressPolicy(request)))

    def secret_uri(self, secret: str, key: str) -> str:
        """The credentialUri of a key in a Secret of the workspace.

        The Secret needs the label ``prokube.ai/sandbox-credential=true``.
        """
        return SECRET_URI.format(workspace=self._atespace(), secret=secret, key=key)

    async def presets(self) -> Presets:
        """The presets the platform offers and its default preset."""
        url = self._presets_url
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

    async def list_actors(self, *, all_workspaces: bool = False) -> list[Actor]:
        """Return every Actor of the workspace, sandboxes or not.

        With all_workspaces, those of all Atespaces the caller may list.
        """
        atespace = "" if all_workspaces else self._atespace()  # "" lists all
        request = ateapi.ListActorsRequest(atespace=atespace)
        return await _list_all(self._control.ListActors, request, "actors")

    async def workers(self) -> list[Worker]:
        """Return Substrate's Workers, the nodes that run Actors."""
        request = ateapi.ListWorkersRequest()
        return await _list_all(self._control.ListWorkers, request, "workers")

    async def worker_actors(self, worker: str) -> list[ActorAssignment]:
        """Return the assignments of Actors to the Worker called worker."""
        request = ateapi.ListWorkerActorAssignmentsRequest(
            worker=ateapi.ObjectRef(name=worker)
        )
        return await _list_all(
            self._control.ListWorkerActorAssignments, request, "actor_assignments"
        )

    async def list(self) -> list[Actor]:
        """Return all sandboxes of the workspace."""
        return [
            actor
            for actor in await self.list_actors()
            if actor.metadata.name.startswith(SANDBOX_PREFIX)
        ]

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
        return self._env.env(actor_name(name), atespace=self._atespace())

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
        self, name: str, path: str, data: FileData, *, mode: int = 0o644
    ) -> int:
        """Write a file, replacing it; returns the number of bytes written.

        The env file API only allows paths under /workspace.
        """
        if isinstance(data, str):
            data = data.encode()
        return await self.env(name).write_file(path, data, mode=mode)

    async def close(self) -> None:
        await self._channel.close()

    async def __aenter__(self) -> AsyncSandboxClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _atespace(self) -> str:
        if not self.workspace:
            raise ValueError(
                "This call needs a workspace: pass workspace, set "
                "PROKUBE_WORKSPACE or run pk login --workspace."
            )
        return self.workspace

    def _ref(self, name: str) -> ateapi.ObjectRef:
        return ateapi.ObjectRef(atespace=self._atespace(), name=actor_name(name))

    async def _egress_policy(self, name: str) -> ateapi.EgressPolicy:
        request = ateapi.GetActorEgressPolicyRequest(
            actor=self._ref(name), name=EGRESS_POLICY_NAME
        )
        return await _call(self._control.GetActorEgressPolicy(request))

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
        endpoint: str | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        async def create_client() -> AsyncSandboxClient:
            return AsyncSandboxClient(
                url, workspace, endpoint=endpoint, token_provider=token_provider
            )

        try:
            self._client = self._run(create_client())
        except BaseException:
            self._stop_loop()
            raise
        self.url = self._client.url
        self.workspace = self._client.workspace
        self.endpoint = self._client.endpoint

    def create(
        self,
        name: str,
        *,
        preset: str | None = None,
        network: Sequence[Mapping[str, Any]] | None = None,
    ) -> Actor:
        """Create a sandbox from a preset (see AsyncSandboxClient)."""
        return self._run(self._client.create(name, preset=preset, network=network))

    def resize(self, name: str, *, preset: str) -> Actor:
        """Switch a sandbox to another preset (see AsyncSandboxClient)."""
        return self._run(self._client.resize(name, preset=preset))

    def network(self, name: str) -> list[dict[str, Any]]:
        """The sandbox's egress rules (see AsyncSandboxClient)."""
        return self._run(self._client.network(name))

    def set_network(
        self, name: str, rules: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace the sandbox's egress rules (see AsyncSandboxClient)."""
        return self._run(self._client.set_network(name, rules))

    def secret_uri(self, secret: str, key: str) -> str:
        return self._client.secret_uri(secret, key)

    def presets(self) -> Presets:
        return self._run(self._client.presets())

    def has_env_guest(self, name: str) -> bool:
        return self._run(self._client.has_env_guest(name))

    def list_actors(self, *, all_workspaces: bool = False) -> list[Actor]:
        return self._run(self._client.list_actors(all_workspaces=all_workspaces))

    def workers(self) -> list[Worker]:
        return self._run(self._client.workers())

    def worker_actors(self, worker: str) -> list[ActorAssignment]:
        return self._run(self._client.worker_actors(worker))

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
        self, name: str, path: str, data: FileData, *, mode: int = 0o644
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


def _rules(policy: ateapi.EgressPolicy) -> list[dict[str, Any]]:
    return [json_format.MessageToDict(rule) for rule in policy.rules]


def _parse_rules(rules: Sequence[Mapping[str, Any]]) -> list[ateapi.EgressRule]:
    return [json_format.ParseDict(dict(rule), ateapi.EgressRule()) for rule in rules]


def _pod_namespace() -> str | None:
    path = Path(NAMESPACE_FILE)
    return path.read_text().strip() if path.is_file() else None


async def _call(awaitable: Awaitable[T]) -> T:
    """Await an ate API call and raise the same errors as the env client."""
    try:
        return await awaitable
    except grpc.RpcError as error:
        raise map_rpc_error(error) from error


async def _list_all(
    method: Callable[[Any], Awaitable[Any]], request: Any, field: str
) -> list[Any]:
    """Call an ate API List method page by page; return field of all pages."""
    items: list[Any] = []
    while True:
        response = await _call(method(request))
        items += getattr(response, field)
        request.options.page_token = response.next_page_token
        if not request.options.page_token:
            return items
