import time
from typing import Any

import grpc
import grpc.aio
import pytest
from ate_env import NotFoundError, ShellResult
from ate_env._gen.ateenv.v1alpha import guest_pb2

from prokube import auth, endpoints
from prokube.sandbox import (
    AsyncSandboxClient,
    SandboxClient,
    Settings,
    SettingsError,
    actor_name,
)
from prokube.sandbox import client as client_module
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import _BearerToken
from prokube.sandbox.settings import build_template
from tests.conftest import make_jwt
from tests.test_settings import (
    CODING,
    IMAGES,
    IMAGES_JSON,
    NGINX,
    OPENCODE_POLICY,
    SIZES,
    SIZES_JSON,
)

URL = "https://platform.example.com"
SIZES_URL = f"{URL}/svc/sandbox/v1alpha/sizes"
IMAGES_URL = f"{URL}/svc/sandbox/v1alpha/images"
SUSPENDED = ateapi.ACTOR_STATE_SUSPENDED
RUNNING = ateapi.ACTOR_STATE_RUNNING


def actor(name: str, atespace: str = "team-a") -> ateapi.Actor:
    return ateapi.Actor(metadata=ateapi.ResourceMetadata(atespace=atespace, name=name))


def rpc_error(code: grpc.StatusCode, details: str) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        code, grpc.aio.Metadata(), grpc.aio.Metadata(), details=details
    )


def not_found(what: str) -> grpc.aio.AioRpcError:
    return rpc_error(grpc.StatusCode.NOT_FOUND, f"{what} not found")


class FakeControl:
    """Stands in for ateapi.Control; records every request.

    Actors and templates put in ``actors`` and ``templates`` behave like
    stored ones: suspend, resume and update change them.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, object]] = []
        self.pages = [ateapi.ListActorsResponse()]
        self.actors: dict[str, ateapi.Actor] = {}
        self.templates: dict[tuple[str, str], ateapi.ActorTemplate] = {}

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    async def CreateActor(self, request):
        self.requests.append(("CreateActor", request))
        self.actors[request.actor.metadata.name] = request.actor
        return request.actor

    async def ListActors(self, request):
        self.requests.append(("ListActors", request))
        return self.pages.pop(0)

    async def GetActor(self, request):
        self.requests.append(("GetActor", request))
        if request.actor.name in self.actors:
            return self.actors[request.actor.name]
        return actor(request.actor.name)

    async def DeleteActor(self, request):
        self.requests.append(("DeleteActor", request))
        return actor(request.actor.name)

    async def SuspendActor(self, request):
        self.requests.append(("SuspendActor", request))
        return ateapi.SuspendActorResponse(actor=self._set_state(request, SUSPENDED))

    async def ResumeActor(self, request):
        self.requests.append(("ResumeActor", request))
        return ateapi.ResumeActorResponse(actor=self._set_state(request, RUNNING))

    async def UpdateActor(self, request):
        self.requests.append(("UpdateActor", request))
        stored = self.actors[request.actor.metadata.name]
        assert request.actor.metadata.uid == stored.metadata.uid
        assert request.actor.metadata.version == stored.metadata.version
        assert stored.status.state == SUSPENDED
        stored.actor_template.CopyFrom(request.actor.actor_template)
        stored.metadata.version += 1
        return stored

    async def CreateActorTemplate(self, request):
        self.requests.append(("CreateActorTemplate", request))
        metadata = request.actor_template.metadata
        self.templates[(metadata.atespace, metadata.name)] = request.actor_template
        return request.actor_template

    async def GetActorTemplate(self, request):
        self.requests.append(("GetActorTemplate", request))
        key = (request.actor_template.atespace, request.actor_template.name)
        if key not in self.templates:
            raise not_found("actor template")
        return self.templates[key]

    async def CreateActorEgressPolicy(self, request):
        self.requests.append(("CreateActorEgressPolicy", request))
        return request.egress_policy

    def _set_state(self, request, state) -> ateapi.Actor:
        if request.actor.name not in self.actors:
            return actor(request.actor.name)
        stored = self.actors[request.actor.name]
        stored.status.state = state
        stored.metadata.version += 1
        return stored


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
        raise not_found("actor")

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
    """A plaintext gRPC server standing in for the in-cluster Agent Gateway.

    Records the request and metadata of each call; streaming calls record
    their first request.
    """
    calls: list[tuple[Any, dict[str, str]]] = []

    async def get_actor(request, context):
        calls.append((request, dict(context.invocation_metadata())))
        return actor(request.actor.name, request.actor.atespace)

    async def read_file(request, context):  # unary-stream
        calls.append((request, dict(context.invocation_metadata())))
        yield guest_pb2.ReadFileResponse(chunk=b"hi")

    async def write_file(request_iterator, context):  # stream-unary
        requests = [request async for request in request_iterator]
        calls.append((requests[0], dict(context.invocation_metadata())))
        return guest_pb2.WriteFileResponse(
            bytes_written=sum(len(request.chunk) for request in requests)
        )

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
            ),
            grpc.method_handlers_generic_handler(
                "ateenv.v1alpha.FileSystemService",
                {
                    "ReadFile": grpc.unary_stream_rpc_method_handler(
                        read_file,
                        request_deserializer=guest_pb2.ReadFileRequest.FromString,
                        response_serializer=guest_pb2.ReadFileResponse.SerializeToString,
                    ),
                    "WriteFile": grpc.stream_unary_rpc_method_handler(
                        write_file,
                        request_deserializer=guest_pb2.WriteFileRequest.FromString,
                        response_serializer=guest_pb2.WriteFileResponse.SerializeToString,
                    ),
                },
            ),
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


async def test_workspace_pod_sends_the_token_on_streaming_calls(
    workload_token, gateway
):
    async with AsyncSandboxClient() as client:
        assert await client.write_file("demo", "/workspace/a.txt", b"hi") == 2
        assert await client.read_file("demo", "/workspace/a.txt") == b"hi"

    [(write, write_metadata), (read, read_metadata)] = gateway
    assert (write.path, read.path) == ("/workspace/a.txt", "/workspace/a.txt")
    for metadata in (write_metadata, read_metadata):
        assert metadata["authorization"] == f"Bearer {workload_token}"
        assert metadata["x-env-id"] == "sbx-demo"


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
        created = client.get("demo")
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


# --- Settings: create, update, sizes and suggested images -----------------

CODING_SETTINGS = Settings(CODING, "small", {"OPENCODE_DISABLE_AUTOUPDATE": "1"})


@pytest.fixture
def platform(httpx_mock):
    """The platform's sizes and suggested images."""
    for url, body in ((SIZES_URL, SIZES_JSON), (IMAGES_URL, IMAGES_JSON)):
        httpx_mock.add_response(url=url, json=body, is_reusable=True, is_optional=True)
    return httpx_mock


@pytest.fixture
def sandbox(client, platform):
    """The client with a running sandbox demo from the coding image."""
    template = build_template("team-a", CODING_SETTINGS, SIZES.get())
    client._control.templates[("team-a", template.metadata.name)] = template
    client._control.actors["sbx-demo"] = ateapi.Actor(
        metadata=ateapi.ResourceMetadata(
            atespace="team-a", name="sbx-demo", uid="u-1", version=3
        ),
        actor_template=ref("team-a", template.metadata.name),
        status=ateapi.ActorStatus(state=RUNNING),
    )
    return client


def ref(atespace: str, name: str) -> ateapi.ObjectRef:
    return ateapi.ObjectRef(atespace=atespace, name=name)


def stored_settings(client, actor: ateapi.Actor) -> Settings:
    key = (actor.actor_template.atespace, actor.actor_template.name)
    return Settings.of(client._control.templates[key], SIZES)


async def test_create_uses_the_default_suggested_image_and_its_network(
    client, platform
):
    created = await client.create("demo")

    name = CODING_SETTINGS.template_name
    control = client._control
    assert control.methods() == [
        "GetActorTemplate",
        "CreateActorTemplate",
        "CreateActor",
        "CreateActorEgressPolicy",
    ]
    assert control.templates[("team-a", name)] == build_template(
        "team-a", CODING_SETTINGS, SIZES.get()
    )
    assert created.metadata == ateapi.ResourceMetadata(
        atespace="team-a", name="sbx-demo"
    )
    assert created.actor_template == ref("team-a", name)
    egress = control.requests[-1][1]
    assert egress.actor == ref("team-a", "sbx-demo")
    assert egress.egress_policy == ateapi.EgressPolicy(
        metadata=ateapi.ResourceMetadata(atespace="team-a", name="default"),
        rules=OPENCODE_POLICY.rules,
    )


async def test_create_reuses_the_template_of_the_same_settings(client, platform):
    first = await client.create("one")
    second = await client.create("two")

    assert first.actor_template == second.actor_template
    assert client._control.methods().count("CreateActorTemplate") == 1


async def test_create_reuses_a_template_created_meanwhile(client, platform):
    async def already_exists(request):
        raise rpc_error(grpc.StatusCode.ALREADY_EXISTS, "actor template exists")

    client._control.CreateActorTemplate = already_exists

    created = await client.create("demo")

    assert created.actor_template.name == CODING_SETTINGS.template_name


async def test_create_applies_size_and_env_to_a_suggested_image(client, platform):
    created = await client.create(
        "demo",
        suggested="python",
        size="medium",
        env={"MODE": "dev", "OPENCODE_DISABLE_AUTOUPDATE": None},
    )

    assert stored_settings(client, created) == Settings(
        CODING, "medium", {"MODE": "dev"}
    )
    egress = client._control.requests[-1][1]
    assert egress.egress_policy.metadata.name == "default"
    assert not egress.egress_policy.rules


async def test_create_with_a_custom_image_has_no_network_rules(
    client, platform, monkeypatch
):
    images = []

    async def canonical(image):
        images.append(image)
        return NGINX

    monkeypatch.setattr(client_module, "canonical_image", canonical)

    created = await client.create("web", image="nginx:1", env_guest=False)

    assert images == ["nginx:1"]
    assert stored_settings(client, created) == Settings(NGINX, "small", env_guest=False)
    assert "CreateActorEgressPolicy" not in client._control.methods()
    assert [str(r.url) for r in platform.get_requests()] == [SIZES_URL]


async def test_create_refuses_a_suggested_and_a_custom_image(client):
    with pytest.raises(SettingsError, match="not both"):
        await client.create("demo", suggested="coding", image=NGINX)

    assert client._control.requests == []


async def test_update_suspends_switches_the_template_and_resumes(sandbox):
    control = sandbox._control

    updated = await sandbox.update("demo", size="medium")

    assert control.methods() == [
        "GetActor",
        "GetActorTemplate",  # the current settings
        "GetActorTemplate",  # the new ones: missing
        "CreateActorTemplate",
        "SuspendActor",
        "UpdateActor",
        "ResumeActor",
    ]
    update = control.requests[5][1]
    assert update.actor.metadata.uid == "u-1"
    assert update.actor.metadata.version == 4  # after the suspend
    medium = CODING_SETTINGS.changed(size="medium")
    assert update.actor.actor_template == ref("team-a", medium.template_name)
    assert updated.status.state == RUNNING
    assert stored_settings(sandbox, updated) == medium


async def test_update_keeps_a_suspended_sandbox_suspended(sandbox):
    control = sandbox._control
    control.actors["sbx-demo"].status.state = SUSPENDED

    updated = await sandbox.update("demo", env={"MODE": "dev"})

    assert updated.status.state == SUSPENDED
    assert control.methods()[-2:] == ["SuspendActor", "UpdateActor"]
    assert stored_settings(sandbox, updated).env == {
        "MODE": "dev",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }


async def test_update_resolves_a_new_image(sandbox, monkeypatch):
    async def canonical(image):
        return NGINX

    monkeypatch.setattr(client_module, "canonical_image", canonical)

    updated = await sandbox.update("demo", image="nginx:1", env_guest=False)

    assert stored_settings(sandbox, updated) == CODING_SETTINGS.changed(
        image=NGINX, env_guest=False
    )


async def test_update_back_reuses_the_earlier_template(sandbox):
    await sandbox.update("demo", size="medium")
    updated = await sandbox.update("demo", size="small")

    assert updated.actor_template == ref("team-a", CODING_SETTINGS.template_name)
    assert sandbox._control.methods().count("CreateActorTemplate") == 1


async def test_update_without_changes_touches_nothing(sandbox):
    updated = await sandbox.update("demo", size="small")

    assert updated.status.state == RUNNING
    assert sandbox._control.methods() == ["GetActor", "GetActorTemplate"]


async def test_update_to_an_unknown_size_touches_nothing(sandbox):
    with pytest.raises(SettingsError, match="Unknown size 'huge'"):
        await sandbox.update("demo", size="huge")

    assert "SuspendActor" not in sandbox._control.methods()


async def test_settings_are_read_from_the_sandboxs_template(sandbox):
    assert await sandbox.settings("demo") == CODING_SETTINGS


async def test_sizes_are_read_with_the_callers_token(client, httpx_mock):
    httpx_mock.add_response(
        url=SIZES_URL, match_headers={"authorization": "Bearer token"}, json=SIZES_JSON
    )

    assert await client.sizes() == SIZES


async def test_unreadable_images_are_a_clear_error(client, httpx_mock):
    httpx_mock.add_response(url=IMAGES_URL, status_code=404, text="route not found")

    with pytest.raises(SettingsError) as error:
        await client.images()

    assert str(error.value) == (
        f"Cannot read the sandbox images from {IMAGES_URL}: HTTP 404"
    )


def test_platform_config_in_a_workspace_pod_is_the_in_cluster_gateway():
    assert endpoints.sandbox_config_url(None, "images") == (
        "http://agentgateway-proxy.agentgateway-system.svc.cluster.local:80"
        "/svc/sandbox/v1alpha/images"
    )


def test_sync_client_wraps_the_settings_calls(platform):
    with sync_client() as client:
        medium = client.create("demo", size="medium").actor_template.name
        updated = client.update("demo", size="small")

        assert client.sizes() == SIZES
        assert client.images() == IMAGES
        assert client.settings("demo") == CODING_SETTINGS
        assert medium == CODING_SETTINGS.changed(size="medium").template_name
        assert updated.actor_template.name == CODING_SETTINGS.template_name
