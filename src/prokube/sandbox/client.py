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
from ate_env import Env, NotFoundError, RpcError, ShellResult, map_rpc_error

from prokube import auth, endpoints
from prokube.auth import TokenProvider
from prokube.sandbox import templates
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox._ateapi.ateapi_pb2_grpc import ControlStub
from prokube.sandbox.images import resolve_image
from prokube.sandbox.templates import (
    ActorTemplate,
    Presets,
    Size,
    Sizes,
    TemplateError,
)

SANDBOX_PREFIX = "sbx-"
# Every Actor's EgressPolicy is a singleton with this name, as in pkui.
EGRESS_POLICY_NAME = "default"

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
    ``workspace`` to ``$PROKUBE_WORKSPACE``. ``token_provider`` defaults to the
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
        url = url or os.environ.get("PROKUBE_URL")
        workspace = workspace or os.environ.get("PROKUBE_WORKSPACE")
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

    async def create(
        self,
        name: str,
        *,
        preset: str | None = None,
        image: str | None = None,
        size: str | None = None,
        env: Mapping[str, str] | None = None,
        env_guest: bool = True,
    ) -> Actor:
        """Create a sandbox from a preset or from a custom image.

        A preset (default: the platform's default one) starts fast and gives
        the sandbox its network rules. A custom ``image`` gets the workspace
        template ``<name>-v<n>`` at ``size`` (default: the platform's default
        size) with literal ``env``, readable by every workspace member. It
        starts slower, has no network rules, and its tag is resolved to a
        digest where the registry allows anonymous pulls. Commands, files,
        the terminal and SSH work only with images based on
        pk-sandbox-coding; for other images pass ``env_guest=False``. The
        sandbox starts SUSPENDED and wakes on its first command or on resume().
        """
        network = None
        if image is None:
            if size or env or not env_guest:
                raise TemplateError("Size, env and env guest need a custom image.")
            chosen = (await self.presets()).get(preset)
            template_ref = ateapi.ObjectRef(
                atespace=endpoints.TEMPLATE_ATESPACE, name=chosen.template
            )
            network = chosen.network
        elif preset:
            raise TemplateError("Choose a preset or a custom image, not both.")
        else:
            template_ref = await self._create_template(
                name, image=image, size=size, env=env, env_guest=env_guest
            )
        actor = ateapi.Actor(
            metadata=ateapi.ResourceMetadata(
                atespace=self.workspace, name=actor_name(name)
            ),
            actor_template=template_ref,
        )
        request = ateapi.CreateActorRequest(actor=actor)
        created = await _call(self._control.CreateActor(request))
        if network is not None:
            await self._create_egress_policy(name, network)
        return created

    async def resize(
        self, name: str, *, preset: str | None = None, size: str | None = None
    ) -> Actor:
        """Switch a sandbox to another preset with the same image, or another size.

        Another size uses the size variant of the sandbox's template (created
        if missing; the template itself at its own size). A running sandbox
        is suspended first and resumed afterwards: /workspace is kept,
        running processes restart. A suspended sandbox stays suspended.
        Network rules and credentials stay.
        """
        if (preset is None) == (size is None):
            raise TemplateError("Pass either a preset or a size.")
        actor = await self.get(name)
        if preset is not None:
            target = await self._preset_with_same_image(actor, preset)
        else:
            sizes = await self.sizes()
            base = await self._base_template(actor.actor_template, sizes)
            target = await self._at_size(base, sizes.get(size), sizes)
        if target == actor.actor_template:
            return actor
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
        return Presets.from_json(await self._platform_config("presets"))

    async def sizes(self) -> Sizes:
        """The sandbox sizes the platform offers and its default size."""
        return Sizes.from_json(await self._platform_config("sizes"))

    async def template(self, name: str) -> ActorTemplate:
        """The ActorTemplate a sandbox runs on: its image, size and env."""
        actor = await self.get(name)
        return await self._template(actor.actor_template)

    async def has_env_guest(self, name: str) -> bool:
        """Whether the sandbox's image is based on pk-sandbox-coding.

        Only then do run, read_file, write_file, env(), the terminal and SSH work.
        """
        return templates.has_env_guest(await self.template(name))

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

    async def _template(self, ref: ateapi.ObjectRef) -> ActorTemplate:
        request = ateapi.GetActorTemplateRequest(actor_template=ref)
        return await _call(self._control.GetActorTemplate(request))

    async def _create_template(
        self,
        name: str,
        *,
        image: str,
        size: str | None,
        env: Mapping[str, str] | None,
        env_guest: bool,
    ) -> ateapi.ObjectRef:
        """Create ``<workspace>/<name>-v<n>``, the next version of name.

        name must be neither a preset's nor a shared template's family and
        must leave room for ``-v<n>-<size>`` within 63 characters.
        """
        sizes, presets = await self.sizes(), await self.presets()
        shared = await self._list_templates(endpoints.TEMPLATE_ATESPACE)
        existing = await self._list_templates(self.workspace)
        versioned = templates.next_version(name, [t.metadata.name for t in existing])
        templates.check_name(
            name, versioned, [t.metadata.name for t in shared], presets, sizes
        )
        template = templates.build_template(
            self.workspace,
            versioned,
            image=await resolve_image(image),
            size=sizes.get(size),
            env=env,
            env_guest=env_guest,
        )
        request = ateapi.CreateActorTemplateRequest(actor_template=template)
        await _call(self._control.CreateActorTemplate(request))
        return ateapi.ObjectRef(atespace=self.workspace, name=versioned)

    async def _create_egress_policy(
        self, name: str, network: ateapi.EgressPolicy
    ) -> None:
        """Give a new sandbox its preset's network rules.

        The policy sync may have applied the same rules first; that is fine.
        """
        policy = ateapi.EgressPolicy(
            metadata=ateapi.ResourceMetadata(
                atespace=self.workspace, name=EGRESS_POLICY_NAME
            ),
            rules=network.rules,
        )
        request = ateapi.CreateActorEgressPolicyRequest(
            actor=self._ref(name), egress_policy=policy
        )
        try:
            await _call(self._control.CreateActorEgressPolicy(request))
        except RpcError as error:
            if error.code != grpc.StatusCode.ALREADY_EXISTS:
                raise

    async def _preset_with_same_image(
        self, actor: Actor, preset: str
    ) -> ateapi.ObjectRef:
        """The preset's template ref; refused if its image is not the sandbox's."""
        chosen = (await self.presets()).get(preset)
        ref = ateapi.ObjectRef(
            atespace=endpoints.TEMPLATE_ATESPACE, name=chosen.template
        )
        current = await self._template(actor.actor_template)
        target = await self._template(ref)
        if current.containers[0].image != target.containers[0].image:
            raise TemplateError(
                f"Preset {preset!r} has another image than this sandbox; resizing "
                "keeps the image. Create a new sandbox from the preset instead."
            )
        return ref

    async def _platform_config(self, name: str) -> Any:
        url = endpoints.sandbox_config_url(self.url, name)
        token = await asyncio.to_thread(self._token_provider)
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(url, headers={"authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise TemplateError(
                f"Cannot read the sandbox {name} from {url}: HTTP {response.status_code}"
            )
        return response.json()

    async def _list_templates(self, atespace: str) -> list[ActorTemplate]:
        found: list[ActorTemplate] = []
        page_token = ""
        while True:
            request = ateapi.ListActorTemplatesRequest(
                atespace=atespace, options=ateapi.ListOptions(page_token=page_token)
            )
            response = await _call(self._control.ListActorTemplates(request))
            found += response.actor_templates
            page_token = response.next_page_token
            if not page_token:
                return found

    async def _at_size(
        self, ref: ateapi.ObjectRef, size: Size, sizes: Sizes
    ) -> ateapi.ObjectRef:
        """The template ref at size: itself if it has that size, else its variant.

        The variant ``<workspace>/<template>-<size>`` is created if missing.
        """
        template = await self._template(ref)
        if sizes.of(template) == size:
            return ref
        variant = templates.with_size(template, size)
        variant_ref = ateapi.ObjectRef(
            atespace=self.workspace, name=templates.variant_name(ref.name, size)
        )
        variant.metadata.CopyFrom(
            ateapi.ResourceMetadata(
                atespace=variant_ref.atespace, name=variant_ref.name
            )
        )
        try:
            existing = await self._template(variant_ref)
        except NotFoundError:
            request = ateapi.CreateActorTemplateRequest(actor_template=variant)
            await _call(self._control.CreateActorTemplate(request))
            return variant_ref
        if sizes.of(existing) != size or not templates.same_except_size(
            existing, template
        ):
            raise TemplateError(
                f"{variant_ref.atespace}/{variant_ref.name} exists but is not "
                f"{ref.atespace}/{ref.name} at size {size.name}."
            )
        return variant_ref

    async def _base_template(
        self, ref: ateapi.ObjectRef, sizes: Sizes
    ) -> ateapi.ObjectRef:
        """The template a size variant was made from; any other template itself.

        A variant is ``<workspace>/<T>-<size>`` that differs from ``<workspace>/<T>``
        or ``sandbox-templates/<T>`` only in its size.
        """
        if ref.atespace != self.workspace:
            return ref
        for size in sizes.sizes:
            if not ref.name.endswith(f"-{size.name}"):
                continue
            variant = await self._template(ref)
            stem = ref.name.removesuffix(f"-{size.name}")
            for atespace in (self.workspace, endpoints.TEMPLATE_ATESPACE):
                candidate = ateapi.ObjectRef(atespace=atespace, name=stem)
                try:
                    template = await self._template(candidate)
                except NotFoundError:
                    continue
                if templates.same_except_size(template, variant):
                    return candidate
        return ref


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

    def create(
        self,
        name: str,
        *,
        preset: str | None = None,
        image: str | None = None,
        size: str | None = None,
        env: Mapping[str, str] | None = None,
        env_guest: bool = True,
    ) -> Actor:
        """Create a sandbox from a preset or a custom image (see AsyncSandboxClient)."""
        return self._run(
            self._client.create(
                name,
                preset=preset,
                image=image,
                size=size,
                env=env,
                env_guest=env_guest,
            )
        )

    def resize(
        self, name: str, *, preset: str | None = None, size: str | None = None
    ) -> Actor:
        """Switch a sandbox to another preset or size (see AsyncSandboxClient)."""
        return self._run(self._client.resize(name, preset=preset, size=size))

    def presets(self) -> Presets:
        return self._run(self._client.presets())

    def sizes(self) -> Sizes:
        return self._run(self._client.sizes())

    def template(self, name: str) -> ActorTemplate:
        return self._run(self._client.template(name))

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
