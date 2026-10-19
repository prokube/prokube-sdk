from unittest.mock import AsyncMock, call

import pytest
from ate_env import NotFoundError, ShellResult
from click.testing import CliRunner
from mcp import Client

from prokube.cli import commands
from prokube.cli.mcp_server import create_server
from prokube.sandbox import AsyncSandboxClient
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi


def actor(name: str, state: str = "ACTOR_STATE_SUSPENDED") -> ateapi.Actor:
    return ateapi.Actor(
        metadata=ateapi.ResourceMetadata(atespace="team-a", name=name),
        status=ateapi.ActorStatus(state=state),
    )


@pytest.fixture
def sandboxes() -> AsyncMock:
    client = AsyncMock(spec=AsyncSandboxClient)
    client.list.return_value = [actor("sbx-a"), actor("sbx-b", "ACTOR_STATE_RUNNING")]
    client.create.return_value = actor("sbx-a")
    client.run.return_value = ShellResult(stdout="42\n", stderr="", exit_code=0)
    client.read_file.return_value = b"hello"
    client.write_file.return_value = 5
    client.suspend.return_value = actor("sbx-a")
    client.resume.return_value = actor("sbx-a", "ACTOR_STATE_RUNNING")
    client.delete.return_value = actor("sbx-a", "ACTOR_STATE_DELETING")
    return client


async def call_tool(sandboxes: AsyncMock, tool: str, **arguments):
    async with Client(create_server(lambda: sandboxes)) as client:
        return await client.call_tool(tool, arguments)


@pytest.mark.parametrize(
    ("tool", "arguments", "method", "expected_call", "result"),
    [
        (
            "list",
            {},
            "list",
            call(),
            {
                "result": [
                    {"name": "a", "state": "SUSPENDED"},
                    {"name": "b", "state": "RUNNING"},
                ]
            },
        ),
        (
            "create",
            {"name": "a"},
            "create",
            call("a", preset=None),
            {"name": "a", "state": "SUSPENDED"},
        ),
        (
            "create",
            {"name": "a", "preset": "pk-default-large"},
            "create",
            call("a", preset="pk-default-large"),
            {"name": "a", "state": "SUSPENDED"},
        ),
        (
            "run",
            {"name": "a", "command": "echo 42"},
            "run",
            call("a", "echo 42", timeout=300),
            {"stdout": "42\n", "stderr": "", "exit_code": 0},
        ),
        (
            "run",
            {"name": "a", "command": "make", "timeout": 10},
            "run",
            call("a", "make", timeout=10),
            {"stdout": "42\n", "stderr": "", "exit_code": 0},
        ),
        (
            "read_file",
            {"name": "a", "path": "/workspace/x"},
            "read_file",
            call("a", "/workspace/x"),
            {"result": "hello"},
        ),
        (
            "write_file",
            {"name": "a", "path": "/workspace/x", "content": "hello"},
            "write_file",
            call("a", "/workspace/x", "hello"),
            {"result": 5},
        ),
        (
            "suspend",
            {"name": "a"},
            "suspend",
            call("a"),
            {"name": "a", "state": "SUSPENDED"},
        ),
        (
            "resume",
            {"name": "a"},
            "resume",
            call("a"),
            {"name": "a", "state": "RUNNING"},
        ),
        (
            "delete",
            {"name": "a"},
            "delete",
            call("a"),
            {"name": "a", "state": "DELETING"},
        ),
    ],
)
async def test_each_tool_is_one_client_call(
    sandboxes, tool, arguments, method, expected_call, result
):
    response = await call_tool(sandboxes, tool, **arguments)

    assert not response.is_error
    assert response.structured_content == result
    assert getattr(sandboxes, method).await_args_list == [expected_call]


async def test_tools_are_the_client_methods(sandboxes):
    async with Client(create_server(lambda: sandboxes)) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools.tools] == [
        "list",
        "create",
        "run",
        "read_file",
        "write_file",
        "suspend",
        "resume",
        "delete",
    ]


async def test_errors_reach_the_caller_as_text(sandboxes):
    sandboxes.read_file.side_effect = NotFoundError("/workspace/x does not exist")

    response = await call_tool(sandboxes, "read_file", name="a", path="/workspace/x")

    assert response.is_error
    assert "NotFoundError: /workspace/x does not exist" in str(response.content)


async def test_client_is_made_once(sandboxes):
    connects = []

    def connect():
        connects.append(1)
        return sandboxes

    async with Client(create_server(connect)) as client:
        assert connects == []
        await client.call_tool("list", {})
        await client.call_tool("list", {})

    assert connects == [1]


def test_mcp_needs_a_login_outside_a_pod():
    result = CliRunner().invoke(commands.cli, ["sandbox", "mcp"])

    assert result.exit_code == 2
    assert "No platform URL. Run: pk login <url>" in result.output
