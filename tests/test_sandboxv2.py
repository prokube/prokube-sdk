"""Tests for the Sandbox v2 (Firecracker) client and facade.

All HTTP is mocked (pytest_httpx) — no live cluster required. Mirrors the v1
sandbox tests: covers create (fc-host AND fc-pod), run_code, commands, files,
pause/resume, get, kill, wait_until_ready polling, the api-key path prefix, and
claiming a warm-pool member via from_pool.
"""

import base64
import json

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError, SandboxTimeoutError
from prokube.sandboxv2 import SandboxV2, SandboxV2Client
from prokube.sandboxv2.models import SandboxV2Status

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
        # First refresh -> Pending, second -> Running.
        httpx_mock.add_response(
            method="GET", url=f"{COLL}/sbx", json=_sandbox_json(name="sbx", phase="Pending")
        )
        httpx_mock.add_response(
            method="GET", url=f"{COLL}/sbx", json=_sandbox_json(name="sbx", phase="Running")
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
            method="GET", url=f"{COLL}/sbx", json=_sandbox_json(name="sbx", phase="Failed")
        )
        sbx = SandboxV2.create(image="pk-sandbox-base", name="sbx")
        with pytest.raises(SandboxError):
            sbx.wait_until_ready(timeout=10)

    def test_wait_until_ready_timeout(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=COLL, status_code=201, json=_sandbox_json(phase="Pending")
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{COLL}/sbx",
            json=_sandbox_json(name="sbx", phase="Pending"),
            is_reusable=True,
        )
        sbx = SandboxV2.create(image="pk-sandbox-base", name="sbx")
        with pytest.raises(SandboxTimeoutError):
            sbx.wait_until_ready(timeout=0)


# ---------------------------------------------------------------------------
# API-key path prefix + no warm pool
# ---------------------------------------------------------------------------


class TestApiKeyAndPool:
    def test_api_key_preserves_path_prefix(self, httpx_mock: HTTPXMock):
        cfg = Config(api_url="https://prokube.ai/pkui", workspace=NS, api_key="k")
        # No version check under api-key auth; the header is x-api-key.
        httpx_mock.add_response(
            method="GET",
            url=f"https://prokube.ai/pkui/api/namespaces/{NS}/sandboxv2/sbx",
            json=_sandbox_json(name="sbx"),
        )
        client = SandboxV2Client(cfg)
        info = client.get("sbx")
        req = httpx_mock.get_requests()[-1]
        assert req.headers["x-api-key"] == "k"
        assert info.name == "sbx"
        client.close()

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
            req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
            assert str(req.url).endswith(
                f"/api/namespaces/{NS}/sandboxv2-pools/python-pool/claim"
            )
        finally:
            sbx._client.close()
