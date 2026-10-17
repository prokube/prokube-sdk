"""Thin personal-Sandbox client on the upstream env SDK. No new executor/API."""

from __future__ import annotations

import asyncio
import os
import re
import threading
from dataclasses import dataclass

import grpc
import httpx
from ate_env import Client as EnvClient
from ate_env import errors as env_errors
from ate_env._gen.ateenv.v1alpha import env_pb2

from .auth import Auth
from .config import Profile, Profiles
from .errors import (
    AuthenticationRequired,
    ConnectionFailed,
    PermissionDenied,
    SandboxAlreadyExists,
    SandboxError,
    SandboxNotFound,
)
from .policy import EgressRule
from .secrets import MAX_SECRET_BYTES, check_environment, secret_command


def check_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,46}[a-z0-9])?", name):
        raise SandboxError(
            "Sandbox name: 1–48 lowercase letters/digits/hyphens, starting and ending with a letter or digit."
        )
    return name


def translate(error: Exception) -> SandboxError:
    if isinstance(error, env_errors.NotFoundError):
        return SandboxNotFound(
            "Sandbox or requested resource not found in your personal space."
        )
    if isinstance(error, env_errors.PermissionDeniedError):
        return PermissionDenied("This operation is not allowed for your identity.")
    code = getattr(error, "code", None)
    if callable(code):
        code = code()
    if code == grpc.StatusCode.UNAUTHENTICATED:
        return AuthenticationRequired(
            "Your session is invalid or expired. Run pk login."
        )
    if code == grpc.StatusCode.ALREADY_EXISTS:
        return SandboxAlreadyExists(
            "Sandbox already exists. Use pk sandbox connect <name>."
        )
    return SandboxError("Sandbox operation failed: " + str(error))


async def invoke(awaitable):
    try:
        return await awaitable
    except (env_errors.EnvError, grpc.RpcError) as error:
        raise translate(error) from error


@dataclass(frozen=True)
class SandboxInfo:
    id: str
    status: str


class _Metadata(grpc.AuthMetadataPlugin):
    def __init__(self, token):
        self.token = token

    def __call__(self, context, callback):
        try:
            callback((("authorization", "Bearer " + self.token()),), None)
        except Exception:
            callback((), RuntimeError("Authentication unavailable. Run pk login."))


class AsyncSandboxClient:
    """Personal Sandboxes using the active pk profile and shared credentials."""

    def __init__(
        self, profile: str | Profile | None = None, *, api_key: str | None = None
    ):
        self.profile = (
            profile if isinstance(profile, Profile) else Profiles().load(profile)
        )
        self.auth = Auth(self.profile)
        self._api_key = api_key or os.environ.get("PROKUBE_API_KEY")
        self._channel = None
        self._env = None
        self._closed = False
        self._http = httpx.AsyncClient(base_url=self.profile.api_url + "/", timeout=90)

    def token(self):
        return self._api_key or self.auth.token()

    async def _ready(self):
        if self._closed:
            raise SandboxError("This client is closed.")
        # Fail with a useful exception before entering the gRPC credential plugin.
        await asyncio.to_thread(self.token)
        if self._channel is None:
            credentials = grpc.composite_channel_credentials(
                grpc.ssl_channel_credentials(),
                grpc.metadata_call_credentials(_Metadata(self.token)),
            )
            self._channel = grpc.aio.secure_channel(
                self.profile.grpc_target, credentials
            )
            self._env = EnvClient(channel=self._channel)
        assert self._env is not None
        return self._env

    async def request(self, method, path, **kwargs):
        token = await asyncio.to_thread(self.token)
        try:
            response = await self._http.request(
                method,
                path.lstrip("/"),
                headers={"Authorization": "Bearer " + token},
                **kwargs,
            )
        except httpx.HTTPError as error:
            raise ConnectionFailed("Cannot reach the Sandbox API.") from error
        errors = {
            401: AuthenticationRequired("Run pk login to renew your session."),
            403: PermissionDenied("Access denied."),
            404: SandboxNotFound("Sandbox not found."),
            409: SandboxAlreadyExists("Sandbox already exists."),
        }
        if response.status_code in errors:
            if response.status_code == 409 and path.endswith("/egress"):
                raise SandboxError(
                    "Egress policy changed concurrently. Read it again before applying."
                )
            raise errors[response.status_code]
        if response.status_code >= 400:
            if response.status_code == 412 and path.endswith("/resume"):
                raise SandboxError(
                    "Sandbox cannot resume in its current state. For CRASHED Sandboxes, explicitly run pk sandbox revert NAME to restore the last snapshot (newer changes are discarded), then resume."
                )
            if response.status_code == 412 and path.endswith("/revert"):
                raise SandboxError(
                    "Revert requires a CRASHED Sandbox with a completed durable snapshot. Check pk sandbox list; recovery may require native administration."
                )
            if response.status_code == 412 and path.endswith("/egress"):
                raise SandboxError(
                    "Policy needs native administration, or HTTPS interception requires a new trust-enabled Sandbox."
                )
            raise SandboxError(f"Sandbox API returned HTTP {response.status_code}.")
        return response.json() if response.content else None

    async def create(self, name: str):
        upstream = await self._ready()
        env = await invoke(upstream.create(check_name(name), template_name="coding-v1"))
        return AsyncSandbox(self, env)

    async def egress_options(self):
        return await self.request("GET", "v1/egress-options")

    async def get(self, name: str):
        upstream = await self._ready()
        await invoke(upstream.get(check_name(name)))
        return AsyncSandbox(self, upstream.env(name))

    async def list(self) -> list[SandboxInfo]:
        result, cursor, seen = [], "", set()
        while True:
            page = await self.request(
                "GET", "v1/sandboxes", params={"page_token": cursor}
            )
            if not isinstance(page, dict) or not isinstance(
                page.get("sandboxes"), list
            ):
                raise ConnectionFailed("Invalid listing response from the platform.")
            for item in page["sandboxes"]:
                state = env_pb2.EnvironmentStatus.Name(
                    item.get("status", 0)
                ).removeprefix("ENVIRONMENT_STATUS_")
                result.append(SandboxInfo(item["id"], state))
            cursor = page.get("next_page_token", "")
            if not cursor:
                return result
            if cursor in seen:
                raise SandboxError("Server repeated a pagination cursor.")
            seen.add(cursor)

    async def close(self):
        if not self._closed:
            self._closed = True
            if self._channel:
                await self._channel.close()
            await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class AsyncSandbox:
    def __init__(self, client, env):
        self._client, self._env = client, env
        self.id = env.id

    async def run(self, command: str, *, stdin=None, timeout=None, env=None):
        check_environment(env)
        await self._client._ready()
        return await invoke(
            self._env.shell(command, stdin=stdin, timeout=timeout, env=env)
        )

    async def start_process(self, command: list[str], **options):
        check_environment(options.get("env"))
        await self._client._ready()
        return AsyncProcess(await invoke(self._env.start_process(command, **options)))

    async def write_file(self, path, data, **options):
        await self._client._ready()
        await invoke(self._env.write_file(path, data, **options))

    async def read_file(self, path):
        await self._client._ready()
        return await invoke(self._env.read_file_bytes(path))

    async def read_chunks(self, path):
        await self._client._ready()
        try:
            async for chunk in self._env.read_file(path):
                yield chunk
        except env_errors.EnvError as error:
            raise translate(error) from error

    async def info(self):
        await self._client._ready()
        return await invoke(self._env.info())

    async def suspend(self):
        await self._client._ready()
        await invoke(self._env.suspend())

    async def resume(self):
        return await self._client.request("POST", f"v1/sandboxes/{self.id}/resume")

    async def revert(self):
        """Discard crashed execution and return to the saved snapshot, SUSPENDED."""
        return await self._client.request("POST", f"v1/sandboxes/{self.id}/revert")

    async def delete(self):
        await self._client._ready()
        await invoke(self._env.delete())

    async def get_egress(self):
        return await self._client.request("GET", f"v1/sandboxes/{self.id}/egress")

    async def set_egress(
        self, rules: list[EgressRule | dict], *, revision: str | None = None
    ):
        if revision is None:
            revision = (await self.get_egress())["revision"]
        return await self._client.request(
            "PUT",
            f"v1/sandboxes/{self.id}/egress",
            json={
                "revision": revision,
                "rules": [
                    r.to_dict() if isinstance(r, EgressRule) else r for r in rules
                ],
            },
        )

    async def write_secret(self, name: str, value: bytes) -> str:
        """Atomically write a developer-owned 0600 file; guest/snapshots can read it."""
        if not isinstance(value, bytes) or len(value) > MAX_SECRET_BYTES:
            raise SandboxError("Secret must be bytes, at most 1 MiB.")
        command, path = secret_command(name, "write")
        result = await self.run(command, stdin=value, timeout=30)
        if result.exit_code:
            raise SandboxError("Secret file delivery failed.")
        return path

    async def remove_secret(self, name: str):
        command, _ = secret_command(name, "remove")
        result = await self.run(command, timeout=30)
        if result.exit_code:
            raise SandboxError("Secret file removal failed.")


class AsyncProcess:
    def __init__(self, process):
        self._process = process
        self.id = process.id

    async def output(self, **options):
        try:
            async for item in self._process.output(**options):
                yield item
        except env_errors.EnvError as error:
            raise translate(error) from error

    async def wait(self):
        return await invoke(self._process.wait())

    async def info(self):
        return await invoke(self._process.info())

    async def write_input(self, data, **options):
        return await invoke(self._process.write_input(data, **options))

    async def signal(self, sig):
        return await invoke(self._process.signal(sig))

    async def kill(self):
        return await invoke(self._process.kill())


class _Loop:
    """Dedicated loop: sync API is also usable inside notebooks' running loops."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def call(self, coro):
        if not self.thread.is_alive():
            coro.close()
            raise SandboxError("Client is closed.")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def iterate(self, iterator):
        try:
            while True:
                try:
                    yield self.call(iterator.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            if self.thread.is_alive():
                self.call(iterator.aclose())

    def close(self):
        self.call(self.loop.shutdown_asyncgens())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()


class SandboxClient:
    """Synchronous facade over AsyncSandboxClient; closing does not delete Sandboxes."""

    def __init__(self, profile: str | Profile | None = None, *, api_key=None):
        # Resolve configuration before starting a thread so constructor failures
        # cannot leak an event loop.
        resolved = profile if isinstance(profile, Profile) else Profiles().load(profile)
        self._loop = _Loop()

        async def make():
            return AsyncSandboxClient(resolved, api_key=api_key)

        try:
            self._async = self._loop.call(make())
        except BaseException:
            self._loop.close()
            raise
        self.profile = resolved
        self._closed = False

    def create(self, name):
        return Sandbox(self, self._loop.call(self._async.create(name)))

    def get(self, name):
        return Sandbox(self, self._loop.call(self._async.get(name)))

    def list(self):
        return self._loop.call(self._async.list())

    def egress_options(self):
        return self._loop.call(self._async.egress_options())

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._loop.call(self._async.close())
            finally:
                self._loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Sandbox:
    def __init__(self, client, sandbox):
        self._client, self._async = client, sandbox
        self.id = sandbox.id

    def run(self, command, **options):
        return self._client._loop.call(self._async.run(command, **options))

    def start_process(self, command, **options):
        return Process(
            self._client._loop,
            self._client._loop.call(self._async.start_process(command, **options)),
        )

    def write_file(self, path, data, **options):
        return self._client._loop.call(self._async.write_file(path, data, **options))

    def read_file(self, path):
        return self._client._loop.call(self._async.read_file(path))

    def read_chunks(self, path):
        return self._client._loop.iterate(self._async.read_chunks(path))

    def info(self):
        return self._client._loop.call(self._async.info())

    def suspend(self):
        return self._client._loop.call(self._async.suspend())

    def resume(self):
        return self._client._loop.call(self._async.resume())

    def revert(self):
        return self._client._loop.call(self._async.revert())

    def delete(self):
        return self._client._loop.call(self._async.delete())

    def get_egress(self):
        return self._client._loop.call(self._async.get_egress())

    def set_egress(self, rules, *, revision=None):
        return self._client._loop.call(self._async.set_egress(rules, revision=revision))

    def write_secret(self, name: str, value: bytes) -> str:
        return self._client._loop.call(self._async.write_secret(name, value))

    def remove_secret(self, name: str):
        return self._client._loop.call(self._async.remove_secret(name))


class Process:
    def __init__(self, loop, process):
        self._loop, self._async = loop, process
        self.id = process.id

    def output(self, **options):
        return self._loop.iterate(self._async.output(**options))

    def wait(self):
        return self._loop.call(self._async.wait())

    def info(self):
        return self._loop.call(self._async.info())

    def write_input(self, data, **options):
        return self._loop.call(self._async.write_input(data, **options))

    def signal(self, sig):
        return self._loop.call(self._async.signal(sig))

    def kill(self):
        return self._loop.call(self._async.kill())
