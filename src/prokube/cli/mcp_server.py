"""pk sandbox mcp: the sandbox client as MCP tools.

Each tool is one AsyncSandboxClient call with the server's credentials, so
every caller acts as that identity. The server keeps no state of its own.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ate_env import ShellResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from prokube.sandbox import AsyncSandboxClient
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import SANDBOX_PREFIX

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass(frozen=True)
class Sandbox:
    name: str
    state: str


def create_server(connect: Callable[[], AsyncSandboxClient]) -> MCPServer:
    """Return the MCP server; connect() makes its client on the first call."""
    server = MCPServer("prokube-sandbox")
    client: AsyncSandboxClient | None = None

    def sandboxes() -> AsyncSandboxClient:
        nonlocal client
        if client is None:
            client = connect()  # in the server's event loop, as the client needs
        return client

    def tool(name: str) -> Callable[[F], F]:
        """Register a tool whose errors reach the caller as text."""

        def register(function: F) -> F:
            @functools.wraps(function)
            async def call(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await function(*args, **kwargs)
                except Exception as error:
                    raise ToolError(f"{type(error).__name__}: {error}") from error

            server.tool(name=name)(call)
            return function

        return register

    @tool("list")
    async def list_() -> list[Sandbox]:
        """List the sandboxes of the workspace."""
        return [_sandbox(actor) for actor in await sandboxes().list()]

    @tool("create")
    async def create(name: str, preset: str | None = None) -> Sandbox:
        """Create a sandbox from a preset (default: the platform's default).

        It starts suspended and wakes on its first command.
        """
        return _sandbox(await sandboxes().create(name, preset=preset))

    @tool("run")
    async def run(name: str, command: str, timeout: float = 300) -> ShellResult:
        """Run `sh -c command` in the sandbox and wait for it to exit.

        After timeout seconds the command is killed.
        """
        return await sandboxes().run(name, command, timeout=timeout)

    @tool("read_file")
    async def read_file(name: str, path: str) -> str:
        """Read a text file under /workspace."""
        data = await sandboxes().read_file(name, path)
        return data.decode(errors="replace")

    @tool("write_file")
    async def write_file(name: str, path: str, content: str) -> int:
        """Write a text file under /workspace, replacing it. Returns its size."""
        return await sandboxes().write_file(name, path, content)

    @tool("suspend")
    async def suspend(name: str) -> Sandbox:
        """Snapshot a running sandbox and free its resources."""
        return _sandbox(await sandboxes().suspend(name))

    @tool("resume")
    async def resume(name: str) -> Sandbox:
        """Start a suspended sandbox from its latest snapshot."""
        return _sandbox(await sandboxes().resume(name))

    @tool("delete")
    async def delete(name: str) -> Sandbox:
        """Delete a sandbox and its /workspace."""
        return _sandbox(await sandboxes().delete(name))

    return server


def _sandbox(actor: ateapi.Actor) -> Sandbox:
    return Sandbox(
        name=actor.metadata.name.removeprefix(SANDBOX_PREFIX),
        state=ateapi.ActorState.Name(actor.status.state).removeprefix("ACTOR_STATE_"),
    )
