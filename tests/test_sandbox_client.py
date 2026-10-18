import time

import grpc
import grpc.aio
import pytest
from ate_env import NotFoundError, ShellResult

from prokube import auth, endpoints
from prokube.sandbox import AsyncSandboxClient, SandboxClient, actor_name
from prokube.sandbox import client as client_module
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import _BearerToken
from tests.conftest import make_jwt

URL = "https://platform.example.com"


def actor(name: str, atespace: str = "team-a") -> ateapi.Actor:
    return ateapi.Actor(metadata=ateapi.ResourceMetadata(atespace=atespace, name=name))


class FakeControl:
    """Stands in for ateapi.Control; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, object]] = []
        self.pages = [ateapi.ListActorsResponse()]

    async def CreateActor(self, request):
        self.requests.append(("CreateActor", request))
        return request.actor

    async def ListActors(self, request):
        self.requests.append(("ListActors", request))
        return self.pages.pop(0)

    async def GetActor(self, request):
        self.requests.append(("GetActor", request))
        return actor(request.actor.name)

    async def DeleteActor(self, request):
        self.requests.append(("DeleteActor", request))
        return actor(request.actor.name)

    async def SuspendActor(self, request):
        self.requests.append(("SuspendActor", request))
        return ateapi.SuspendActorResponse(actor=actor(request.actor.name))

    async def ResumeActor(self, request):
        self.requests.append(("ResumeActor", request))
        return ateapi.ResumeActorResponse(actor=actor(request.actor.name))


class FakeEnv:
    """Stands in for an ate_env.Env handle."""

    def __init__(self, id: str, atespace: str) -> None:
        self.id = id
        self.atespace = atespace
        self.calls: list[tuple] = []
        self.files: dict[str, bytes] = {}

    async def shell(self, command, **kwargs):
        self.calls.append(("shell", command, kwargs))
        return ShellResult(stdout="hi\n", stderr="", exit_code=0)

    async def read_file_bytes(self, path):
        return self.files[path]

    async def write_file(self, path, data, *, mode):
        self.files[path] = data
        self.calls.append(("write_file", path, mode))
        return len(data)


class FakeEnvClient:
    """Stands in for ate_env.Client; hands out one FakeEnv per sandbox."""

    def __init__(self) -> None:
        self.envs: dict[tuple[str, str], FakeEnv] = {}

    def env(self, id: str, *, atespace: str) -> FakeEnv:
        return self.envs.setdefault((id, atespace), FakeEnv(id, atespace))


@pytest.fixture
async def client():
    client = AsyncSandboxClient(URL, "team-a", token_provider=lambda: "token")
    client._control = FakeControl()  # type: ignore[assignment]
    client._env = FakeEnvClient()  # type: ignore[assignment]
    yield client
    await client.close()


def test_sandbox_names_get_the_sbx_prefix_once():
    assert actor_name("demo") == "sbx-demo"
    assert actor_name("sbx-demo") == "sbx-demo"


async def test_create_makes_an_actor_in_the_workspace_atespace(client):
    created = await client.create("demo", template="python-v1")

    [(method, request)] = client._control.requests
    assert method == "CreateActor"
    assert created.metadata.atespace == "team-a"
    assert created.metadata.name == "sbx-demo"
    assert created.actor_template.atespace == endpoints.TEMPLATE_ATESPACE
    assert created.actor_template.name == "python-v1"


async def test_create_accepts_a_template_from_another_atespace(client):
    created = await client.create("demo", template="mine", template_atespace="team-a")

    assert created.actor_template == ateapi.ObjectRef(atespace="team-a", name="mine")


async def test_list_follows_pages_and_keeps_only_sandboxes(client):
    client._control.pages = [
        ateapi.ListActorsResponse(
            actors=[actor("sbx-one"), actor("session-123")], next_page_token="p2"
        ),
        ateapi.ListActorsResponse(actors=[actor("sbx-two")]),
    ]

    sandboxes = await client.list()

    assert [s.metadata.name for s in sandboxes] == ["sbx-one", "sbx-two"]
    tokens = [r.options.page_token for _, r in client._control.requests]
    assert tokens == ["", "p2"]
    assert all(r.atespace == "team-a" for _, r in client._control.requests)


async def test_lifecycle_calls_address_the_sandbox_actor(client):
    await client.get("demo")
    await client.suspend("demo")
    resumed = await client.resume("sbx-demo")
    await client.delete("demo")

    ref = ateapi.ObjectRef(atespace="team-a", name="sbx-demo")
    requests = client._control.requests
    assert [method for method, _ in requests] == [
        "GetActor",
        "SuspendActor",
        "ResumeActor",
        "DeleteActor",
    ]
    assert all(request.actor == ref for _, request in requests)
    assert requests[-1][1].any_state is True
    assert resumed.metadata.name == "sbx-demo"


async def test_ate_api_errors_become_env_client_errors(client):
    async def missing(request):
        raise grpc.aio.AioRpcError(
            grpc.StatusCode.NOT_FOUND,
            grpc.aio.Metadata(),
            grpc.aio.Metadata(),
            details="actor not found",
        )

    client._control.GetActor = missing

    with pytest.raises(NotFoundError, match="actor not found"):
        await client.get("demo")


async def test_commands_and_files_go_through_the_env_client(client):
    result = await client.run("demo", "echo hi", timeout=30)
    written = await client.write_file("demo", "/workspace/a.txt", "hello")
    content = await client.read_file("demo", "/workspace/a.txt")

    env = client._env.envs[("sbx-demo", "team-a")]
    assert result.stdout == "hi\n"
    assert env.calls[0] == (
        "shell",
        "echo hi",
        {"stdin": None, "cwd": "", "env": None, "timeout": 30},
    )
    assert env.calls[1] == ("write_file", "/workspace/a.txt", 0o644)
    assert written == 5
    assert content == b"hello"
    assert client.env("demo") is env


async def test_url_and_workspace_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROKUBE_URL", URL + "/")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "team-b")

    async with AsyncSandboxClient(token_provider=lambda: "token") as client:
        assert client.url == URL
        assert client.workspace == "team-b"


async def test_url_and_workspace_are_required():
    with pytest.raises(ValueError, match="PROKUBE_WORKSPACE"):
        AsyncSandboxClient(URL)


async def test_default_credentials_are_the_stored_login():
    with pytest.raises(auth.LoginRequired):
        AsyncSandboxClient(URL, "team-a")

    auth._save_tokens(
        URL, {"id_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600}
    )
    async with AsyncSandboxClient(URL, "team-a") as client:
        assert client.workspace == "team-a"


@pytest.fixture
def workload_token(tmp_path, monkeypatch) -> str:
    """A workspace pod: a workload token and namespace team-pod."""
    token = make_jwt(time.time() + 3600)
    (tmp_path / "token").write_text(token)
    (tmp_path / "namespace").write_text("team-pod\n")
    monkeypatch.setenv("PROKUBE_SANDBOX_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setattr(client_module, "NAMESPACE_FILE", str(tmp_path / "namespace"))
    return token


@pytest.fixture
async def gateway(monkeypatch):
    """A plaintext gRPC server standing in for the in-cluster Agent Gateway."""
    calls: list[tuple[ateapi.GetActorRequest, dict[str, str]]] = []

    async def get_actor(request, context):
        calls.append((request, dict(context.invocation_metadata())))
        return actor(request.actor.name, request.actor.atespace)

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                "ateapi.Control",
                {
                    "GetActor": grpc.unary_unary_rpc_method_handler(
                        get_actor,
                        request_deserializer=ateapi.GetActorRequest.FromString,
                        response_serializer=ateapi.Actor.SerializeToString,
                    )
                },
            )
        ]
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    monkeypatch.setattr(endpoints, "IN_CLUSTER_GATEWAY", f"127.0.0.1:{port}")
    yield calls
    await server.stop(None)


async def test_workspace_pod_needs_no_configuration(workload_token, gateway):
    async with AsyncSandboxClient() as client:
        assert client.url is None
        assert client.workspace == "team-pod"
        await client.get("demo")

    [(request, metadata)] = gateway
    assert request.actor == ateapi.ObjectRef(atespace="team-pod", name="sbx-demo")
    assert metadata["authorization"] == f"Bearer {workload_token}"


async def test_workspace_pod_can_name_another_workspace(workload_token, gateway):
    async with AsyncSandboxClient(workspace="team-b") as client:
        await client.get("demo")

    assert gateway[0][0].actor.atespace == "team-b"


async def test_with_a_url_the_workload_token_comes_after_the_login(workload_token):
    async with AsyncSandboxClient(URL) as client:
        assert client.url == URL
        assert client.workspace == "team-pod"
        assert client._ate_channel is not None


async def test_without_url_or_workload_token_configuration_is_required():
    with pytest.raises(ValueError, match="PROKUBE_URL"):
        AsyncSandboxClient(token_provider=lambda: "token")


def call_plugin(plugin: _BearerToken) -> tuple:
    """Invoke the gRPC auth plugin the way gRPC does and return what it sent."""
    sent = []
    plugin(None, lambda metadata, error: sent.append((metadata, error)))  # type: ignore[arg-type]
    return sent[0]


def test_bearer_token_is_asked_for_on_every_call():
    tokens = iter(["first", "second"])
    plugin = _BearerToken(lambda: next(tokens))

    assert call_plugin(plugin) == ((("authorization", "Bearer first"),), None)
    assert call_plugin(plugin) == ((("authorization", "Bearer second"),), None)


def test_bearer_token_failure_fails_the_call():
    def expired() -> str:
        raise auth.LoginRequired("expired")

    metadata, error = call_plugin(_BearerToken(expired))

    assert metadata == ()
    assert isinstance(error, auth.LoginRequired)


def sync_client() -> SandboxClient:
    client = SandboxClient(URL, "team-a", token_provider=lambda: "token")
    client._client._control = FakeControl()  # type: ignore[assignment]
    client._client._env = FakeEnvClient()  # type: ignore[assignment]
    return client


def test_sync_client_wraps_the_async_one():
    with sync_client() as client:
        created = client.create("demo", template="python-v1")
        result = client.run("demo", "echo hi")
        client.write_file("demo", "/workspace/a.txt", b"x")

        assert created.metadata.name == "sbx-demo"
        assert result.exit_code == 0
        assert client.read_file("demo", "/workspace/a.txt") == b"x"
        assert client.list() == []
        assert client.workspace == "team-a"


async def test_sync_client_works_inside_a_running_event_loop():
    # As in a Jupyter notebook, where a loop is already running.
    with sync_client() as client:
        assert client.get("demo").metadata.name == "sbx-demo"


def test_sync_client_reports_construction_errors():
    with pytest.raises(auth.LoginRequired):
        SandboxClient(URL, "team-a")
