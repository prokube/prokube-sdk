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
    PresetError,
    SandboxClient,
    actor_name,
    inject,
    passthrough,
)
from prokube.sandbox import client as client_module
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import _BearerToken
from tests.conftest import make_jwt
from tests.test_presets import OPENCODE_POLICY, PRESETS, PRESETS_JSON

URL = "https://platform.example.com"
PRESETS_URL = f"{URL}/svc/sandbox/v1alpha/presets"
SHARED = "sandbox-templates"
CODING = "registry.example.com/pk-sandbox-coding@sha256:" + "a" * 64
OTHER_IMAGE = "registry.example.com/other@sha256:" + "b" * 64
SUSPENDED = ateapi.ACTOR_STATE_SUSPENDED
RUNNING = ateapi.ACTOR_STATE_RUNNING


def actor(name: str, atespace: str = "team-a") -> ateapi.Actor:
    return ateapi.Actor(metadata=ateapi.ResourceMetadata(atespace=atespace, name=name))


def template(
    name: str,
    image: str = CODING,
    fidelity: ateapi.SnapshotFidelity = ateapi.SNAPSHOT_FIDELITY_VOLUMES,
    env_guest: bool = True,
) -> ateapi.ActorTemplate:
    """A shared template with what resize and the env guest check read."""
    guest = ateapi.Container(name="guest", image=image)
    if env_guest:
        guest.wakeup_probe.http_get.CopyFrom(
            ateapi.HttpGetAction(path="/readyz", port=80)
        )
    return ateapi.ActorTemplate(
        metadata=ateapi.ResourceMetadata(atespace=SHARED, name=name),
        containers=[guest],
        snapshot_config=ateapi.SnapshotConfig(preferred_fidelity=fidelity),
    )


class FakeControl:
    """Stands in for ateapi.Control; records every request.

    Actors in ``actors`` behave like stored ones: create adds them, suspend,
    resume and update change them. ``templates`` are the presets' templates.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, object]] = []
        self.pages = [ateapi.ListActorsResponse()]
        self.actors: dict[str, ateapi.Actor] = {}
        self.egress = ateapi.EgressPolicy()
        memory = ateapi.SNAPSHOT_FIDELITY_MEMORY
        self.templates: dict[str, ateapi.ActorTemplate] = {}
        for size in ("small", "medium", "large"):
            plain, kept = f"pk-default-{size}-v1", f"pk-default-{size}-memory-v1"
            self.templates[plain] = template(plain)
            self.templates[kept] = template(kept, fidelity=memory)

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    async def CreateActor(self, request):
        self.requests.append(("CreateActor", request))
        stored = self.actors[request.actor.metadata.name] = ateapi.Actor()
        stored.CopyFrom(request.actor)
        return request.actor

    async def CreateActorEgressPolicy(self, request):
        self.requests.append(("CreateActorEgressPolicy", request))
        return request.egress_policy

    async def GetActorEgressPolicy(self, request):
        self.requests.append(("GetActorEgressPolicy", request))
        return self.egress

    async def UpdateActorEgressPolicy(self, request):
        self.requests.append(("UpdateActorEgressPolicy", request))
        # Substrate needs the UID and version of the stored policy.
        assert request.egress_policy.metadata == self.egress.metadata
        return request.egress_policy

    async def ListActors(self, request):
        self.requests.append(("ListActors", request))
        return self.pages.pop(0)

    async def GetActor(self, request):
        self.requests.append(("GetActor", request))
        return self.actors.get(request.actor.name) or actor(request.actor.name)

    async def GetActorTemplate(self, request):
        self.requests.append(("GetActorTemplate", request))
        return self.templates[request.actor_template.name]

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


# --- Presets: create and resize ---------------------------------------------


def ref(atespace: str, name: str) -> ateapi.ObjectRef:
    return ateapi.ObjectRef(atespace=atespace, name=name)


@pytest.fixture
def presets_api(httpx_mock):
    httpx_mock.add_response(
        url=PRESETS_URL, json=PRESETS_JSON, is_reusable=True, is_optional=True
    )
    return httpx_mock


@pytest.fixture
def sandbox(client, presets_api):
    """The client with a running sandbox demo from the small preset."""
    client._control.actors["sbx-demo"] = ateapi.Actor(
        metadata=ateapi.ResourceMetadata(
            atespace="team-a", name="sbx-demo", uid="u-1", version=3
        ),
        actor_template=ref(SHARED, "pk-default-small-v1"),
        status=ateapi.ActorStatus(state=RUNNING),
    )
    return client


async def test_create_from_the_default_preset_sets_its_network(client, presets_api):
    created = await client.create("demo")

    assert client._control.methods() == ["CreateActor", "CreateActorEgressPolicy"]
    assert created.metadata == ateapi.ResourceMetadata(
        atespace="team-a", name="sbx-demo"
    )
    assert created.actor_template == ref(SHARED, "pk-default-small-v1")
    egress = client._control.requests[-1][1]
    assert egress.actor == ref("team-a", "sbx-demo")
    assert egress.egress_policy == ateapi.EgressPolicy(
        metadata=ateapi.ResourceMetadata(atespace="team-a", name="default"),
        rules=OPENCODE_POLICY.rules,
    )


STORED_EGRESS = ateapi.EgressPolicy(
    metadata=ateapi.ResourceMetadata(
        atespace="team-a", name="default", uid="p-1", version=7
    ),
    rules=[
        ateapi.EgressRule(
            tls_passthrough=ateapi.TlsPassthroughRule(
                hostnames=["pypi.org"], ports=ateapi.Ports(numbers=[443])
            )
        )
    ],
)


async def test_network_is_the_rules_as_json(client):
    client._control.egress = STORED_EGRESS

    rules = await client.network("demo")

    assert rules == [
        {"tlsPassthrough": {"hostnames": ["pypi.org"], "ports": {"numbers": [443]}}}
    ]
    assert client._control.requests == [
        (
            "GetActorEgressPolicy",
            ateapi.GetActorEgressPolicyRequest(
                actor=ref("team-a", "sbx-demo"), name="default"
            ),
        )
    ]


async def test_set_network_replaces_the_rules(client):
    client._control.egress = STORED_EGRESS
    key = client.secret_uri("anthropic", "key")
    rules = [
        inject("api.anthropic.com", "x-api-key", key),
        inject("api.github.com", "Authorization", key, prefix="Bearer "),
        passthrough("github.com", "registry.npmjs.org"),
    ]

    # The builders' JSON survives the round trip through the proto unchanged.
    assert await client.set_network("demo", rules) == rules
    assert client._control.methods() == [
        "GetActorEgressPolicy",
        "UpdateActorEgressPolicy",
    ]
    assert client._control.requests[-1][1].actor == ref("team-a", "sbx-demo")


def test_rule_builders():
    assert passthrough("github.com", port=22) == {
        "tlsPassthrough": {"hostnames": ["github.com"], "ports": {"numbers": [22]}}
    }
    assert inject("api.example.com", "x-api-key", "ate-secret://x") == {
        "https": {
            "hostnames": ["api.example.com"],
            "ports": {"numbers": [443]},
            "effects": {
                "replaceHeaders": [
                    {"header": "x-api-key", "credentialUri": "ate-secret://x"}
                ]
            },
        }
    }


def test_secret_uri_names_a_secret_of_the_workspace():
    client = SandboxClient(URL, "team-a", token_provider=lambda: "token")

    assert client.secret_uri("anthropic", "key") == (
        "ate-secret://k8s.io/default/team-a/anthropic/key"
    )
    client.close()


async def test_create_from_a_named_preset(client, presets_api):
    created = await client.create("demo", preset="pk-default-large")

    assert created.actor_template == ref(SHARED, "pk-default-large-v1")


async def test_create_keeps_an_existing_egress_policy(client, presets_api):
    async def already_exists(request):
        raise grpc.aio.AioRpcError(
            grpc.StatusCode.ALREADY_EXISTS,
            grpc.aio.Metadata(),
            grpc.aio.Metadata(),
            details="egress policy exists",
        )

    client._control.CreateActorEgressPolicy = already_exists

    created = await client.create("demo")

    assert created.actor_template == ref(SHARED, "pk-default-small-v1")


async def test_create_from_an_unknown_preset_creates_nothing(client, presets_api):
    with pytest.raises(PresetError, match="Unknown preset 'rust'"):
        await client.create("demo", preset="rust")

    assert client._control.requests == []


async def test_resize_suspends_switches_the_preset_and_resumes(sandbox):
    control = sandbox._control

    resized = await sandbox.resize("demo", preset="pk-default-medium")

    assert control.methods() == [
        "GetActor",
        "GetActorTemplate",
        "GetActorTemplate",
        "SuspendActor",
        "UpdateActor",
        "ResumeActor",
    ]
    update = control.requests[4][1]
    assert update.actor.metadata.uid == "u-1"
    assert update.actor.metadata.version == 4  # after the suspend
    assert update.actor.actor_template == ref(SHARED, "pk-default-medium-v1")
    assert resized.status.state == RUNNING
    assert resized.actor_template == ref(SHARED, "pk-default-medium-v1")


async def test_resize_keeps_a_suspended_sandbox_suspended(sandbox):
    control = sandbox._control
    control.actors["sbx-demo"].status.state = SUSPENDED

    resized = await sandbox.resize("demo", preset="pk-default-medium")

    assert resized.status.state == SUSPENDED
    assert control.methods()[-2:] == ["SuspendActor", "UpdateActor"]


async def test_resize_to_the_current_preset_changes_nothing(sandbox):
    resized = await sandbox.resize("demo", preset="pk-default-small")

    assert resized.status.state == RUNNING
    assert sandbox._control.methods() == ["GetActor"]


async def test_resize_refuses_another_snapshot_fidelity(sandbox):
    with pytest.raises(PresetError) as error:
        await sandbox.resize("demo", preset="pk-default-medium-memory")

    assert str(error.value) == (
        "Preset 'pk-default-medium-memory' has another snapshot fidelity than "
        "sandbox 'demo'. Create a new sandbox from it instead."
    )
    assert "SuspendActor" not in sandbox._control.methods()


async def test_resize_refuses_another_image(sandbox):
    sandbox._control.templates["pk-default-large-v1"] = template(
        "pk-default-large-v1", OTHER_IMAGE
    )

    with pytest.raises(PresetError, match="has another image than sandbox 'demo'"):
        await sandbox.resize("demo", preset="pk-default-large")

    assert "SuspendActor" not in sandbox._control.methods()


async def test_resize_works_within_the_memory_presets(sandbox):
    sandbox._control.actors["sbx-demo"].actor_template.CopyFrom(
        ref(SHARED, "pk-default-small-memory-v1")
    )

    resized = await sandbox.resize("demo", preset="pk-default-large-memory")

    assert resized.actor_template == ref(SHARED, "pk-default-large-memory-v1")


async def test_has_env_guest_reads_the_sandboxs_template(sandbox):
    assert await sandbox.has_env_guest("demo")

    sandbox._control.templates["pk-default-small-v1"] = template(
        "pk-default-small-v1", env_guest=False
    )
    assert not await sandbox.has_env_guest("demo")


async def test_presets_are_read_with_the_callers_token(client, httpx_mock):
    httpx_mock.add_response(
        url=PRESETS_URL,
        match_headers={"authorization": "Bearer token"},
        json=PRESETS_JSON,
    )

    assert await client.presets() == PRESETS


async def test_unreadable_presets_are_a_clear_error(client, httpx_mock):
    httpx_mock.add_response(url=PRESETS_URL, status_code=404, text="route not found")

    with pytest.raises(PresetError) as error:
        await client.presets()

    assert str(error.value) == (
        f"Cannot read the sandbox presets from {PRESETS_URL}: HTTP 404"
    )


def test_presets_url_in_a_workspace_pod_is_the_in_cluster_gateway():
    assert endpoints.presets_url(None) == (
        "http://agentgateway-proxy.agentgateway-system.svc.cluster.local:80"
        "/svc/sandbox/v1alpha/presets"
    )


def test_sync_client_wraps_the_preset_calls(presets_api):
    with sync_client() as client:
        created = client.create("demo")
        resized = client.resize("demo", preset="pk-default-medium")

        assert client.presets() == PRESETS
        assert created.actor_template == ref(SHARED, "pk-default-small-v1")
        assert resized.actor_template == ref(SHARED, "pk-default-medium-v1")
        assert client.has_env_guest("demo")
