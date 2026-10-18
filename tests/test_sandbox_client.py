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
    TemplateError,
    actor_name,
    templates,
)
from prokube.sandbox import client as client_module
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import _BearerToken
from tests.conftest import make_jwt
from tests.test_templates import IMAGE, SIZES, SIZES_JSON

URL = "https://platform.example.com"
SIZES_URL = f"{URL}/svc/sandbox/v1alpha/sizes"
SUSPENDED = ateapi.ACTOR_STATE_SUSPENDED
RUNNING = ateapi.ACTOR_STATE_RUNNING


def actor(name: str, atespace: str = "team-a") -> ateapi.Actor:
    return ateapi.Actor(metadata=ateapi.ResourceMetadata(atespace=atespace, name=name))


def not_found(what: str) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        grpc.StatusCode.NOT_FOUND,
        grpc.aio.Metadata(),
        grpc.aio.Metadata(),
        details=f"{what} not found",
    )


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

    async def ListActorTemplates(self, request):
        """Pages of two templates; the page token is the offset."""
        self.requests.append(("ListActorTemplates", request))
        found = [t for (a, _), t in self.templates.items() if a == request.atespace]
        start = int(request.options.page_token or 0)
        more = start + 2 < len(found)
        return ateapi.ListActorTemplatesResponse(
            actor_templates=found[start : start + 2],
            next_page_token=str(start + 2) if more else "",
        )

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


# --- Templates, sizes and resize ------------------------------------------


def approved(name: str = "coding-v2", image: str = IMAGE) -> ateapi.ActorTemplate:
    """An approved template at the default size, like coding-v2."""
    return templates.build_template(
        endpoints.TEMPLATE_ATESPACE, name, image=image, size=SIZES.get()
    )


@pytest.fixture
def sizes_api(httpx_mock):
    httpx_mock.add_response(
        url=SIZES_URL, json=SIZES_JSON, is_reusable=True, is_optional=True
    )
    return httpx_mock


@pytest.fixture
def with_templates(client, sizes_api):
    """The client with sandbox-templates/coding-v2 and a sandbox demo on it."""
    client._control.templates[(endpoints.TEMPLATE_ATESPACE, "coding-v2")] = approved()
    client._control.actors["sbx-demo"] = ateapi.Actor(
        metadata=ateapi.ResourceMetadata(
            atespace="team-a", name="sbx-demo", uid="u-1", version=3
        ),
        actor_template=ateapi.ObjectRef(
            atespace=endpoints.TEMPLATE_ATESPACE, name="coding-v2"
        ),
        status=ateapi.ActorStatus(state=RUNNING),
    )
    return client


def ref(atespace: str, name: str) -> ateapi.ObjectRef:
    return ateapi.ObjectRef(atespace=atespace, name=name)


async def test_sizes_are_read_with_the_callers_token(client, httpx_mock):
    httpx_mock.add_response(
        url=SIZES_URL, match_headers={"authorization": "Bearer token"}, json=SIZES_JSON
    )

    assert await client.sizes() == SIZES


async def test_unreadable_sizes_are_a_clear_error(client, httpx_mock):
    httpx_mock.add_response(url=SIZES_URL, status_code=404, text="route not found")

    with pytest.raises(TemplateError) as error:
        await client.sizes()

    assert (
        str(error.value) == f"Cannot read the sandbox sizes from {SIZES_URL}: HTTP 404"
    )


def test_sizes_url_in_a_workspace_pod_is_the_in_cluster_gateway():
    assert endpoints.sizes_url(None) == (
        "http://agentgateway-proxy.agentgateway-system.svc.cluster.local:80"
        "/svc/sandbox/v1alpha/sizes"
    )


async def test_list_templates_returns_the_approved_and_the_workspaces_own(client):
    stored = client._control.templates
    for name in ("coding-v1", "coding-v2", "python-v2"):
        stored[(endpoints.TEMPLATE_ATESPACE, name)] = approved(name)
    stored[("team-a", "mine-v1")] = approved("mine-v1")
    stored[("team-b", "theirs-v1")] = approved("theirs-v1")

    found = await client.list_templates()

    assert [t.metadata.name for t in found] == [
        "coding-v1",
        "coding-v2",
        "python-v2",
        "mine-v1",
    ]


async def test_create_template_makes_the_next_version(client, sizes_api):
    client._control.templates[("team-a", "mine-v1")] = approved("mine-v1")
    client._control.templates[("team-a", "mine-v2")] = approved("mine-v2")

    created = await client.create_template(
        "mine", image=IMAGE, size="medium", env={"MODE": "dev"}
    )

    assert created == templates.build_template(
        "team-a", "mine-v3", image=IMAGE, size=SIZES.get("medium"), env={"MODE": "dev"}
    )
    assert client._control.methods()[-1] == "CreateActorTemplate"


async def test_create_template_resolves_the_image_and_uses_the_default_size(
    client, sizes_api, monkeypatch
):
    async def resolve(image):
        return f"{image}@sha256:{'d' * 64}"

    monkeypatch.setattr(client_module, "resolve_image", resolve)

    created = await client.create_template("web", image="nginx:1", env_guest=False)

    assert created.metadata.name == "web-v1"
    assert created.containers[0].image == f"nginx:1@sha256:{'d' * 64}"
    assert SIZES.of(created) == SIZES.get("small")
    assert not templates.has_env_guest(created)


async def test_create_with_a_size_creates_the_variant_once(with_templates):
    client = with_templates

    first = await client.create("one", template="coding-v2", size="medium")
    second = await client.create("two", template="coding-v2", size="medium")

    variant_ref = ref("team-a", "coding-v2-medium")
    assert first.actor_template == second.actor_template == variant_ref
    assert client._control.methods().count("CreateActorTemplate") == 1
    variant = client._control.templates[("team-a", "coding-v2-medium")]
    assert SIZES.of(variant) == SIZES.get("medium")
    assert templates.same_except_size(variant, approved())


async def test_create_at_the_templates_own_size_uses_the_template(with_templates):
    created = await with_templates.create("one", template="coding-v2", size="small")

    assert created.actor_template == ref(endpoints.TEMPLATE_ATESPACE, "coding-v2")
    assert "CreateActorTemplate" not in with_templates._control.methods()


async def test_create_refuses_a_variant_name_taken_by_another_template(
    with_templates,
):
    other = templates.with_size(
        approved(image=IMAGE.replace("a" * 64, "b" * 64)), SIZES.get("medium")
    )
    with_templates._control.templates[("team-a", "coding-v2-medium")] = other

    with pytest.raises(TemplateError) as error:
        await with_templates.create("one", template="coding-v2", size="medium")

    assert str(error.value) == (
        "team-a/coding-v2-medium exists but is not sandbox-templates/coding-v2 "
        "at size medium."
    )


async def test_resize_suspends_switches_the_template_and_resumes(with_templates):
    control = with_templates._control

    resized = await with_templates.resize("demo", "medium")

    assert control.methods() == [
        "GetActor",
        "GetActorTemplate",  # the sandbox's template
        "GetActorTemplate",  # its medium variant: missing
        "CreateActorTemplate",
        "SuspendActor",
        "UpdateActor",
        "ResumeActor",
    ]
    update = control.requests[5][1]
    assert update.actor.metadata.uid == "u-1"
    assert update.actor.metadata.version == 4  # after the suspend
    assert update.actor.actor_template == ref("team-a", "coding-v2-medium")
    assert resized.status.state == RUNNING
    assert resized.actor_template == ref("team-a", "coding-v2-medium")


async def test_resize_back_to_the_templates_own_size_uses_the_original(
    with_templates,
):
    control = with_templates._control
    await with_templates.resize("demo", "medium")
    # A custom template with the same name must not be mistaken for the original.
    control.templates[("team-a", "coding-v2")] = approved(
        image=IMAGE.replace("a" * 64, "b" * 64)
    )

    resized = await with_templates.resize("demo", "small")

    assert resized.actor_template == ref(endpoints.TEMPLATE_ATESPACE, "coding-v2")
    assert control.methods().count("CreateActorTemplate") == 1


async def test_resize_to_another_variant_starts_from_the_original(with_templates):
    await with_templates.resize("demo", "medium")

    resized = await with_templates.resize("demo", "gpu")

    assert resized.actor_template == ref("team-a", "coding-v2-gpu")
    variant = with_templates._control.templates[("team-a", "coding-v2-gpu")]
    assert dict(variant.worker_selector.match_labels) == {
        "prokube.ai/sandbox-pool": "gpu"
    }


async def test_resize_to_the_current_size_changes_nothing(with_templates):
    resized = await with_templates.resize("demo", "small")

    assert resized.status.state == RUNNING
    assert with_templates._control.methods() == ["GetActor", "GetActorTemplate"]


async def test_resize_to_an_unknown_size_touches_nothing(with_templates):
    with pytest.raises(TemplateError, match="Unknown size 'huge'"):
        await with_templates.resize("demo", "huge")

    assert "SuspendActor" not in with_templates._control.methods()


async def test_has_env_guest_reads_the_sandboxs_template(with_templates):
    assert await with_templates.has_env_guest("demo")

    plain = templates.build_template(
        "team-a", "web-v1", image=IMAGE, size=SIZES.get(), env_guest=False
    )
    with_templates._control.templates[("team-a", "web-v1")] = plain
    with_templates._control.actors["sbx-demo"].actor_template.CopyFrom(
        ref("team-a", "web-v1")
    )
    assert not await with_templates.has_env_guest("demo")


def test_sync_client_wraps_the_template_calls(sizes_api):
    with sync_client() as client:
        control = client._client._control
        assert isinstance(control, FakeControl)
        control.templates[(endpoints.TEMPLATE_ATESPACE, "coding-v2")] = approved()

        created = client.create_template("mine", image=IMAGE)
        sandbox = client.create("demo", template="coding-v2", size="medium")

        assert client.sizes() == SIZES
        assert [t.metadata.name for t in client.list_templates()] == [
            "coding-v2",
            "mine-v1",
            "coding-v2-medium",
        ]
        assert created.metadata.name == "mine-v1"
        assert sandbox.actor_template == ref("team-a", "coding-v2-medium")
