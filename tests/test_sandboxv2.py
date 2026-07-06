"""Tests for the Sandbox v2 (Firecracker) client and facade.

All HTTP is mocked (pytest_httpx) — no live cluster required. Mirrors the v1
sandbox tests: covers create (fc-host AND fc-pod), run_code, commands, files,
pause/resume, get, kill, wait_until_ready polling, the api-key path prefix, and
claiming a warm-pool member via from_pool.
"""

import base64
import json
import re

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError, SandboxTimeoutError
from prokube.sandboxv2 import SandboxV2, SandboxV2Client, SandboxV2Pool
from prokube.sandboxv2.models import (
    DNSConfig,
    DNSConfigOption,
    ExecAction,
    HTTPGetAction,
    Lifecycle,
    LifecycleHandler,
    Probe,
    SandboxV2Status,
    TCPSocketAction,
)

BASE = "https://test.example.com"
NS = "test-ns"
COLL = f"{BASE}/api/namespaces/{NS}/sandboxv2"


@pytest.fixture
def config():
    return Config(api_url=BASE, workspace=NS, user_id="test-user@example.com")


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("PROKUBE_API_URL", BASE)
    monkeypatch.setenv("PROKUBE_WORKSPACE", NS)
    monkeypatch.setenv("PROKUBE_USER_ID", "test-user@example.com")


def _mock_version(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/version", json={"version": "0.1.0"}
    )


def _sandbox_json(name="sbx", phase="Running", runtime="fc-host"):
    return {
        "name": name,
        "namespace": NS,
        "image": "pk-sandbox-base",
        "runtimeClassName": runtime,
        "phase": phase,
        "operatingMode": "Running",
        "terminalEnabled": True,
    }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_fc_host_body_and_path(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=COLL,
            status_code=201,
            json=_sandbox_json(name="sbx-1", phase="Pending", runtime="fc-host"),
        )
        client = SandboxV2Client(config)
        info = client.create(
            image="pk-sandbox-base",
            name="sbx-1",
            runtime_class="fc-host",
            vcpus=2,
            mem_mib=2048,
            egress=False,
        )

        req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        body = json.loads(req.content)
        assert str(req.url) == COLL
        assert body["name"] == "sbx-1"
        assert body["runtimeClassName"] == "fc-host"
        assert body["vcpus"] == 2
        assert body["memMiB"] == 2048
        assert body["egress"] is False
        assert info.name == "sbx-1"
        assert info.status == SandboxV2Status.PENDING
        assert info.runtime_class == "fc-host"
        client.close()

    def test_create_fc_pod(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=COLL,
            status_code=201,
            json=_sandbox_json(name="sbx-2", phase="Pending", runtime="fc-pod"),
        )
        client = SandboxV2Client(config)
        info = client.create(name="sbx-2", runtime_class="fc-pod")

        body = json.loads(
            [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
        )
        assert body["runtimeClassName"] == "fc-pod"
        # image omitted -> not sent (backend default applies)
        assert "image" not in body
        assert info.runtime_class == "fc-pod"
        client.close()

    def test_create_passthrough_volumes(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        vols = [{"name": "ws", "ephemeral": {"storageClassName": "mayastor"}}]
        mounts = [{"name": "ws", "mountPath": "/workspace"}]
        client.create(name="sbx", volumes=vols, volume_mounts=mounts)

        body = json.loads(
            [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
        )
        assert body["volumes"] == vols
        assert body["volumeMounts"] == mounts
        client.close()

    def test_create_env_vars_and_secret_refs(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            env_vars={"FOO": "bar", "BAZ": "qux"},
            secret_refs=["openai-key", "hf-token"],
        )

        body = json.loads(
            [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
        )
        # dict[str,str] -> CRD spec.env: [{name,value}]
        assert body["env"] == [
            {"name": "FOO", "value": "bar"},
            {"name": "BAZ", "value": "qux"},
        ]
        # list[str] -> CRD spec.envFrom: [{secretRef:{name}}]
        assert body["envFrom"] == [
            {"secretRef": {"name": "openai-key"}},
            {"secretRef": {"name": "hf-token"}},
        ]
        client.close()

    def test_facade_create_env_list_form(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        SandboxV2.create(
            image="pk-sandbox-base",
            name="sbx",
            env_vars=[{"name": "A", "value": "1"}],
            secret_refs=["s1"],
        )
        body = json.loads(
            [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
        )
        assert body["env"] == [{"name": "A", "value": "1"}]
        assert body["envFrom"] == [{"secretRef": {"name": "s1"}}]

    def test_create_omits_env_when_absent(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx")
        body = json.loads(
            [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
        )
        assert "env" not in body
        assert "envFrom" not in body
        client.close()

    def test_facade_create_resources_shorthand(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json(phase="Pending")
        )
        sbx = SandboxV2.create(
            image="pk-sandbox-base", resources={"vcpus": 4, "mem_mib": 4096}
        )
        body = json.loads(
            [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
        )
        assert body["vcpus"] == 4
        assert body["memMiB"] == 4096
        assert sbx.runtime_class == "fc-host"
        assert sbx.status == "Pending"


# ---------------------------------------------------------------------------
# Get / list / kill
# ---------------------------------------------------------------------------


class TestGetListKill:
    def test_get(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=f"{COLL}/sbx", json=_sandbox_json(name="sbx")
        )
        client = SandboxV2Client(config)
        info = client.get("sbx")
        assert info.name == "sbx"
        assert info.status == SandboxV2Status.RUNNING
        client.close()

    def test_list(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=COLL,
            json={
                "sandboxes": [
                    _sandbox_json(name="a", phase="Running"),
                    _sandbox_json(name="b", phase="Paused"),
                ],
                "total": 2,
            },
        )
        client = SandboxV2Client(config)
        infos = client.list()
        assert [i.name for i in infos] == ["a", "b"]
        assert infos[1].status == SandboxV2Status.PAUSED
        client.close()

    def test_kill_deletes(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=f"{COLL}/sbx", json=_sandbox_json(name="sbx")
        )
        httpx_mock.add_response(method="DELETE", url=f"{COLL}/sbx", status_code=204)
        sbx = SandboxV2.get("sbx")
        sbx.kill()
        req = [r for r in httpx_mock.get_requests() if r.method == "DELETE"][-1]
        assert str(req.url) == f"{COLL}/sbx"
        with pytest.raises(SandboxError):
            sbx.run_code("print(1)")


# ---------------------------------------------------------------------------
# Exec: run_code + commands
# ---------------------------------------------------------------------------


class TestExec:
    def test_run_code(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/exec",
            json={
                "stdout": "4\n",
                "stderr": "",
                "exitCode": 0,
                "durationMs": 12,
                "success": True,
                "session_id": "sess-1",
                "sandboxName": "sbx",
            },
        )
        client = SandboxV2Client(config)
        result = client.exec_code("sbx", "print(2+2)", language="python")

        body = json.loads(httpx_mock.get_requests()[-1].content)
        assert body["use_jupyter"] is True
        assert body["language"] == "python"
        assert result.stdout == "4\n"
        assert result.success is True
        assert result.session_id == "sess-1"
        client.close()

    def test_commands_run_uses_bash_shell(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/exec",
            json={
                "stdout": "hello\n",
                "stderr": "",
                "exitCode": 0,
                "durationMs": 5,
                "sandboxName": "sbx",
            },
        )
        client = SandboxV2Client(config)
        result = client.exec_command("sbx", "echo hello")

        body = json.loads(httpx_mock.get_requests()[-1].content)
        assert body["use_jupyter"] is False
        assert body["language"] == "bash"
        assert "session_id" not in body
        assert result.stdout == "hello\n"
        assert result.exit_code == 0
        assert result.success is True
        client.close()

    def test_command_nonzero_exit(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/exec",
            json={"stdout": "", "stderr": "boom", "exitCode": 1, "sandboxName": "sbx"},
        )
        client = SandboxV2Client(config)
        result = client.exec_command("sbx", "false")
        assert result.exit_code == 1
        assert result.success is False
        client.close()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestFiles:
    def test_write_file_base64(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/files",
            json={"message": "ok", "path": "/workspace/x.txt"},
        )
        client = SandboxV2Client(config)
        client.write_file("sbx", "/workspace/x.txt", b"hello world")

        body = json.loads(httpx_mock.get_requests()[-1].content)
        assert body["path"] == "/workspace/x.txt"
        assert body["encoding"] == "base64"
        assert base64.b64decode(body["content"]) == b"hello world"
        client.close()

    def test_read_file(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{COLL}/sbx/files/download?path=%2Fworkspace%2Fx.txt",
            content=b"data",
        )
        client = SandboxV2Client(config)
        assert client.read_file("sbx", "/workspace/x.txt") == b"data"
        client.close()

    def test_list_files_maps_v2_fields(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{COLL}/sbx/files?path=%2Fworkspace",
            json={
                "files": [
                    {
                        "name": "a.txt",
                        "path": "/workspace/a.txt",
                        "size": 3,
                        "isDirectory": False,
                        "modifiedAt": "2026-07-05T00:00:00Z",
                    },
                    {
                        "name": "sub",
                        "path": "/workspace/sub",
                        "size": 0,
                        "isDirectory": True,
                    },
                ],
                "path": "/workspace",
            },
        )
        client = SandboxV2Client(config)
        files = client.list_files("sbx", "/workspace")
        assert files[0].name == "a.txt"
        assert files[0].is_dir is False
        assert files[0].modified == "2026-07-05T00:00:00Z"
        assert files[1].is_dir is True
        client.close()


# ---------------------------------------------------------------------------
# Pause / resume / wait_until_ready
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_pause(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/pause",
            json=_sandbox_json(name="sbx", phase="Paused"),
        )
        client = SandboxV2Client(config)
        info = client.pause("sbx")
        assert info.status == SandboxV2Status.PAUSED
        client.close()

    def test_pause_409_raises_sandbox_error(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/pause",
            status_code=409,
            json={"detail": "not running"},
        )
        client = SandboxV2Client(config)
        with pytest.raises(SandboxError):
            client.pause("sbx")
        client.close()

    def test_resume(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{COLL}/sbx/resume",
            json=_sandbox_json(name="sbx", phase="Running"),
        )
        client = SandboxV2Client(config)
        info = client.resume("sbx")
        assert info.status == SandboxV2Status.RUNNING
        client.close()

    def test_wait_until_ready_polls(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json(phase="Pending")
        )
        # Prefers the server-side long-poll readiness endpoint, which returns
        # the moment the phase reaches Running.
        httpx_mock.add_response(
            method="GET",
            url=re.compile(rf"{re.escape(COLL)}/sbx/wait_ready(\?.*)?$"),
            json=_sandbox_json(name="sbx", phase="Running"),
        )
        sbx = SandboxV2.create(image="pk-sandbox-base", name="sbx")
        sbx.wait_until_ready(timeout=10)
        assert sbx.status == "Running"

    def test_wait_until_ready_failed_raises(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json(phase="Pending")
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(rf"{re.escape(COLL)}/sbx/wait_ready(\?.*)?$"),
            json=_sandbox_json(name="sbx", phase="Failed"),
        )
        sbx = SandboxV2.create(image="pk-sandbox-base", name="sbx")
        with pytest.raises(SandboxError):
            sbx.wait_until_ready(timeout=10)

    def test_wait_until_ready_timeout(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json(phase="Pending")
        )
        # timeout=0 → the deadline has already passed, so no readiness request
        # is issued before it raises.
        sbx = SandboxV2.create(image="pk-sandbox-base", name="sbx")
        with pytest.raises(SandboxTimeoutError):
            sbx.wait_until_ready(timeout=0)


# ---------------------------------------------------------------------------
# API-key ORIGIN routing + warm pool
# ---------------------------------------------------------------------------


class TestApiKeyAndPool:
    def test_api_key_uses_origin_route(self, httpx_mock: HTTPXMock):
        """Under api-key the client targets the top-level v2 ORIGIN route.

        Mirrors v1: HttpClient strips ``api_url`` to its origin, so NO ``/pkui``
        prefix and NO ``/api`` segment — the path is ``/sandboxv2/{ws}/
        sandboxes/{name}`` (this is the fix; the old code wrongly re-attached
        ``/pkui/api/...`` which hit the cookie-gated UI path -> 401).
        """
        cfg = Config(api_url="https://prokube.ai/pkui", workspace=NS, api_key="k")
        # No version check under api-key auth; the header is x-api-key.
        httpx_mock.add_response(
            method="GET",
            url=f"https://prokube.ai/sandboxv2/{NS}/sandboxes/sbx",
            json=_sandbox_json(name="sbx"),
        )
        client = SandboxV2Client(cfg)
        info = client.get("sbx")
        req = httpx_mock.get_requests()[-1]
        assert req.headers["x-api-key"] == "k"
        assert str(req.url) == f"https://prokube.ai/sandboxv2/{NS}/sandboxes/sbx"
        assert info.name == "sbx"
        client.close()

    def test_api_key_exec_and_files_origin_routes(self, httpx_mock: HTTPXMock):
        """exec / files sub-paths also route to the top-level ORIGIN paths."""
        cfg = Config(api_url="https://prokube.ai/pkui", workspace=NS, api_key="k")
        base = f"https://prokube.ai/sandboxv2/{NS}/sandboxes/sbx"
        httpx_mock.add_response(
            method="POST",
            url=f"{base}/exec",
            json={"stdout": "hi\n", "stderr": "", "exitCode": 0, "success": True},
        )
        httpx_mock.add_response(
            method="POST", url=f"{base}/files", json={"message": "ok"}
        )
        client = SandboxV2Client(cfg)
        client.exec_command("sbx", "echo hi")
        client.write_file("sbx", "/workspace/x.txt", b"data")
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert f"{base}/exec" in urls
        assert f"{base}/files" in urls
        client.close()

    def test_api_key_claim_uses_origin_route_with_body(self, httpx_mock: HTTPXMock):
        """Under api-key, claim POSTs to the sandboxes ORIGIN /claim route with
        the pool name in the JSON body (mirrors v1's ``claim_from_pool``)."""
        httpx_mock.add_response(
            method="POST",
            url=f"https://prokube.ai/sandboxv2/{NS}/sandboxes/claim",
            json=_sandbox_json(name="member-1", phase="Running"),
        )
        sbx = SandboxV2.from_pool(
            "python-pool",
            api_url="https://prokube.ai/pkui",
            workspace=NS,
            api_key="k",
        )
        try:
            assert sbx.name == "member-1"
            req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
            assert (
                str(req.url)
                == f"https://prokube.ai/sandboxv2/{NS}/sandboxes/claim"
            )
            body = json.loads(req.content)
            assert body["poolName"] == "python-pool"
        finally:
            sbx._client.close()

    def test_from_pool_signature_matches_v1(self):
        """SandboxV2.from_pool must be drop-in for Sandbox.from_pool."""
        import inspect

        from prokube.sandbox import Sandbox

        v1 = inspect.signature(Sandbox.from_pool)
        v2 = inspect.signature(SandboxV2.from_pool)
        assert list(v1.parameters) == list(v2.parameters)

    def test_from_pool_claims_member(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/{NS}/sandboxv2-pools/python-pool/claim",
            json=_sandbox_json(name="member-1", phase="Running"),
        )
        sbx = SandboxV2.from_pool("python-pool")
        try:
            assert sbx.name == "member-1"
            assert sbx.status == "Running"
            assert sbx.runtime_class == "fc-host"
            assert sbx.workspace == NS
            req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
            assert str(req.url).endswith(
                f"/api/namespaces/{NS}/sandboxv2-pools/python-pool/claim"
            )
        finally:
            sbx._client.close()


# ---------------------------------------------------------------------------
# Declarative startupProbe + lifecycle (RFC declarative-probes-lifecycle)
# ---------------------------------------------------------------------------


POOLS = f"{BASE}/api/namespaces/{NS}/sandboxv2-pools"


def _pool_json(name="pool", size=1, warm_state="Hibernated"):
    return {
        "name": name,
        "namespace": NS,
        "size": size,
        "warmState": warm_state,
        "readyMembers": 0,
        "members": [],
        "image": "pk-sandbox-base",
        "runtimeClassName": "fc-host",
    }


def _last_post_body(httpx_mock: HTTPXMock):
    return json.loads(
        [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
    )


class TestProbesAndLifecycle:
    def test_create_httpget_probe_and_poststart_serialize(
        self, config, httpx_mock: HTTPXMock
    ):
        """A full pk-sandbox-base probe + POST warm-up serializes to the exact
        CRD/back-end camelCase shape (RFC §6)."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            startup_probe=Probe(
                http_get=HTTPGetAction(port=44772, path="/ping"),
                period_seconds=1,
                failure_threshold=120,
            ),
            lifecycle=Lifecycle(
                post_start=LifecycleHandler(
                    http_get=HTTPGetAction(
                        port=44772,
                        path="/code",
                        method="POST",
                        body='{"code":"1"}',
                    )
                )
            ),
        )
        body = _last_post_body(httpx_mock)
        assert body["startupProbe"] == {
            "httpGet": {"port": 44772, "path": "/ping"},
            "periodSeconds": 1,
            "failureThreshold": 120,
        }
        assert body["lifecycle"] == {
            "postStart": {
                "httpGet": {
                    "port": 44772,
                    "path": "/code",
                    "method": "POST",
                    "body": '{"code":"1"}',
                }
            }
        }
        client.close()

    def test_create_tcp_socket_probe(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            startup_probe=Probe(tcp_socket=TCPSocketAction(port=8080)),
        )
        body = _last_post_body(httpx_mock)
        assert body["startupProbe"] == {"tcpSocket": {"port": 8080}}
        client.close()

    def test_create_exec_probe(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            lifecycle=Lifecycle(
                post_start=LifecycleHandler(
                    exec=ExecAction(command=["sh", "-c", "true"])
                )
            ),
        )
        body = _last_post_body(httpx_mock)
        assert body["lifecycle"] == {
            "postStart": {"exec": {"command": ["sh", "-c", "true"]}}
        }
        client.close()

    def test_create_accepts_cr_shaped_dict(self, config, httpx_mock: HTTPXMock):
        """A camelCase CR-shaped dict is accepted and passes through verbatim."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            startup_probe={
                "httpGet": {
                    "port": 9000,
                    "path": "/healthz",
                    "httpHeaders": [{"name": "X-Probe", "value": "1"}],
                },
                "initialDelaySeconds": 3,
            },
        )
        body = _last_post_body(httpx_mock)
        assert body["startupProbe"] == {
            "httpGet": {
                "port": 9000,
                "path": "/healthz",
                "httpHeaders": [{"name": "X-Probe", "value": "1"}],
            },
            "initialDelaySeconds": 3,
        }
        client.close()

    def test_probe_exactly_one_handler_enforced(self):
        # zero handlers
        with pytest.raises(ValueError, match="exactly one handler"):
            Probe(period_seconds=1)
        # two handlers
        with pytest.raises(ValueError, match="exactly one handler"):
            Probe(
                http_get=HTTPGetAction(port=1),
                tcp_socket=TCPSocketAction(port=2),
            )

    def test_lifecycle_handler_exactly_one_handler_enforced(self):
        with pytest.raises(ValueError, match="exactly one handler"):
            LifecycleHandler()
        with pytest.raises(ValueError, match="exactly one handler"):
            LifecycleHandler(
                tcp_socket=TCPSocketAction(port=1),
                exec=ExecAction(command=["true"]),
            )

    def test_omitted_probe_lifecycle_absent_from_json(
        self, config, httpx_mock: HTTPXMock
    ):
        """Back-compat: omitting the fields drops them from the wire (exclude_none),
        so the backend fills the execd default."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx")
        body = _last_post_body(httpx_mock)
        assert "startupProbe" not in body
        assert "lifecycle" not in body
        client.close()

    def test_facade_create_threads_probe(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        sbx = SandboxV2.create(
            name="sbx",
            startup_probe=Probe(tcp_socket=TCPSocketAction(port=7681)),
        )
        body = _last_post_body(httpx_mock)
        assert body["startupProbe"] == {"tcpSocket": {"port": 7681}}
        sbx._client.close()

    def test_pool_template_carries_probe_and_lifecycle(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Pool members declare their own probe/warm-up via the template."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json(name="p")
        )
        pool = SandboxV2Pool.create(
            name="p",
            size=2,
            startup_probe=Probe(
                http_get=HTTPGetAction(port=44772, path="/ping"),
                failure_threshold=120,
            ),
            lifecycle=Lifecycle(
                post_start=LifecycleHandler(
                    http_get=HTTPGetAction(
                        port=44772, path="/code", method="POST", body="{}"
                    )
                )
            ),
        )
        body = _last_post_body(httpx_mock)
        template = body["template"]
        assert template["startupProbe"] == {
            "httpGet": {"port": 44772, "path": "/ping"},
            "failureThreshold": 120,
        }
        assert template["lifecycle"] == {
            "postStart": {
                "httpGet": {
                    "port": 44772,
                    "path": "/code",
                    "method": "POST",
                    "body": "{}",
                }
            }
        }
        pool._client.close()


# ---------------------------------------------------------------------------
# Pod-mirrored guest DNS (spec.dnsPolicy + spec.dnsConfig)
# ---------------------------------------------------------------------------


class TestGuestDNS:
    def test_create_dns_policy_and_config_serialize(
        self, config, httpx_mock: HTTPXMock
    ):
        """dnsPolicy + a full dnsConfig serialize to the exact CRD/back-end shape,
        with options rendered as {name, value} entries."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            dns_policy="None",
            dns_config=DNSConfig(
                nameservers=["1.1.1.1", "8.8.8.8"],
                searches=["svc.cluster.local", "example.com"],
                options=[
                    DNSConfigOption(name="ndots", value="5"),
                    DNSConfigOption(name="edns0"),
                ],
            ),
        )
        body = _last_post_body(httpx_mock)
        assert body["dnsPolicy"] == "None"
        assert body["dnsConfig"] == {
            "nameservers": ["1.1.1.1", "8.8.8.8"],
            "searches": ["svc.cluster.local", "example.com"],
            "options": [
                {"name": "ndots", "value": "5"},
                {"name": "edns0"},
            ],
        }
        client.close()

    def test_create_dns_policy_only(self, config, httpx_mock: HTTPXMock):
        """dnsPolicy alone (no dnsConfig) serializes and omits dnsConfig."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx", dns_policy="ClusterFirst")
        body = _last_post_body(httpx_mock)
        assert body["dnsPolicy"] == "ClusterFirst"
        assert "dnsConfig" not in body
        client.close()

    def test_dns_config_option_value_omitted_when_absent(
        self, config, httpx_mock: HTTPXMock
    ):
        """An option with no value renders as bare {name} (no null value key)."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            dns_config=DNSConfig(options=[DNSConfigOption(name="edns0")]),
        )
        body = _last_post_body(httpx_mock)
        assert body["dnsConfig"] == {"options": [{"name": "edns0"}]}
        client.close()

    def test_create_accepts_cr_shaped_dns_dict(
        self, config, httpx_mock: HTTPXMock
    ):
        """A camelCase CR-shaped dnsConfig dict passes through verbatim."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(
            name="sbx",
            dns_policy="Default",
            dns_config={
                "nameservers": ["10.0.0.10"],
                "options": [{"name": "ndots", "value": "2"}],
            },
        )
        body = _last_post_body(httpx_mock)
        assert body["dnsPolicy"] == "Default"
        assert body["dnsConfig"] == {
            "nameservers": ["10.0.0.10"],
            "options": [{"name": "ndots", "value": "2"}],
        }
        client.close()

    def test_omitted_dns_absent_from_json(self, config, httpx_mock: HTTPXMock):
        """Back-compat: omitting both drops them from the wire (exclude_none),
        so the executor applies its ClusterFirst default."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx")
        body = _last_post_body(httpx_mock)
        assert "dnsPolicy" not in body
        assert "dnsConfig" not in body
        client.close()

    def test_facade_create_threads_dns(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        sbx = SandboxV2.create(
            name="sbx",
            dns_policy="None",
            dns_config=DNSConfig(nameservers=["9.9.9.9"]),
        )
        body = _last_post_body(httpx_mock)
        assert body["dnsPolicy"] == "None"
        assert body["dnsConfig"] == {"nameservers": ["9.9.9.9"]}
        sbx._client.close()

    def test_pool_template_carries_dns(self, mock_env, httpx_mock: HTTPXMock):
        """Pool members declare their own DNS via the template."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json(name="p")
        )
        pool = SandboxV2Pool.create(
            name="p",
            size=2,
            dns_policy="ClusterFirst",
            dns_config=DNSConfig(
                searches=["team.svc.cluster.local"],
                options=[DNSConfigOption(name="ndots", value="3")],
            ),
        )
        body = _last_post_body(httpx_mock)
        template = body["template"]
        assert template["dnsPolicy"] == "ClusterFirst"
        assert template["dnsConfig"] == {
            "searches": ["team.svc.cluster.local"],
            "options": [{"name": "ndots", "value": "3"}],
        }
        pool._client.close()


# ---------------------------------------------------------------------------
# Istio service mesh opt-in (spec.mesh)
# ---------------------------------------------------------------------------


class TestMesh:
    def test_create_mesh_true_serializes(self, config, httpx_mock: HTTPXMock):
        """mesh=True serializes to spec.mesh: true."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx", mesh=True)
        body = _last_post_body(httpx_mock)
        assert body["mesh"] is True
        client.close()

    def test_create_mesh_false_serializes(self, config, httpx_mock: HTTPXMock):
        """mesh=False serializes to spec.mesh: false."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx", mesh=False)
        body = _last_post_body(httpx_mock)
        assert body["mesh"] is False
        client.close()

    def test_omitted_mesh_absent_from_json(self, config, httpx_mock: HTTPXMock):
        """Back-compat: omitting mesh drops it from the wire (exclude_none)."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx")
        body = _last_post_body(httpx_mock)
        assert "mesh" not in body
        client.close()

    def test_facade_create_threads_mesh(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        sbx = SandboxV2.create(name="sbx", mesh=True)
        body = _last_post_body(httpx_mock)
        assert body["mesh"] is True
        sbx._client.close()

    def test_pool_template_carries_mesh(self, mock_env, httpx_mock: HTTPXMock):
        """Pool members declare their own mesh opt-in via the template."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json(name="p")
        )
        pool = SandboxV2Pool.create(name="p", size=2, mesh=True)
        body = _last_post_body(httpx_mock)
        template = body["template"]
        assert template["mesh"] is True
        pool._client.close()


# ---------------------------------------------------------------------------
# Pod-mirrored snapshot resume policy (spec.snapshotResumePolicy)
# ---------------------------------------------------------------------------


class TestSnapshotResumePolicy:
    def test_create_snapshot_resume_policy_serializes(
        self, config, httpx_mock: HTTPXMock
    ):
        """snapshotResumePolicy serializes to the exact CRD/back-end shape."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx", snapshot_resume_policy="AllowStale")
        body = _last_post_body(httpx_mock)
        assert body["snapshotResumePolicy"] == "AllowStale"
        client.close()

    def test_omitted_snapshot_resume_policy_absent_from_json(
        self, config, httpx_mock: HTTPXMock
    ):
        """Back-compat: omitting it drops it from the wire (exclude_none), so
        the executor applies its Strict default."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        client = SandboxV2Client(config)
        client.create(name="sbx")
        body = _last_post_body(httpx_mock)
        assert "snapshotResumePolicy" not in body
        client.close()

    def test_facade_create_threads_snapshot_resume_policy(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json()
        )
        sbx = SandboxV2.create(name="sbx", snapshot_resume_policy="AllowStale")
        body = _last_post_body(httpx_mock)
        assert body["snapshotResumePolicy"] == "AllowStale"
        sbx._client.close()

    def test_pool_template_carries_snapshot_resume_policy(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Pool members declare their own snapshot resume policy via the
        template."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json(name="p")
        )
        pool = SandboxV2Pool.create(
            name="p", size=2, snapshot_resume_policy="AllowStale"
        )
        body = _last_post_body(httpx_mock)
        template = body["template"]
        assert template["snapshotResumePolicy"] == "AllowStale"
        pool._client.close()
