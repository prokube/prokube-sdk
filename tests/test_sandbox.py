"""Tests for Sandbox class."""

import base64

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.exceptions import SandboxError
from prokube.sandbox import Sandbox


@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")
    monkeypatch.setenv("PROKUBE_USER_ID", "test-user@example.com")


class TestSandboxList:
    """Tests for Sandbox.list()."""

    def test_list_empty(self, mock_env, httpx_mock: HTTPXMock):
        """Test listing sandboxes when none exist."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={"sandboxes": [], "total": 0},
        )

        sandboxes = Sandbox.list()

        assert sandboxes == []

    def test_list_multiple(self, mock_env, httpx_mock: HTTPXMock):
        """Test listing multiple sandboxes."""
        # Version check for listing client only (per-sandbox clients skip it)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {
                        "name": "sandbox-1",
                        "namespace": "test-ws",
                        "image": "python:3.10",
                        "phase": "Running",
                        "poolName": "python-pool",
                    },
                    {
                        "name": "sandbox-2",
                        "namespace": "test-ws",
                        "image": "node:18",
                        "phase": "Pending",
                    },
                ],
                "total": 2,
            },
        )

        sandboxes = Sandbox.list()

        assert len(sandboxes) == 2
        assert sandboxes[0].name == "sandbox-1"
        assert sandboxes[0].status == "Running"
        assert sandboxes[0].workspace == "test-ws"
        assert sandboxes[1].name == "sandbox-2"
        assert sandboxes[1].status == "Pending"

        # Clean up
        for sbx in sandboxes:
            sbx._client.close()

    def test_list_single(self, mock_env, httpx_mock: HTTPXMock):
        """Test listing a single sandbox."""
        # Version check for listing client only
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {
                        "name": "my-sandbox",
                        "namespace": "test-ws",
                        "image": "python:3.10",
                        "phase": "Running",
                    },
                ],
                "total": 1,
            },
        )

        sandboxes = Sandbox.list()

        assert len(sandboxes) == 1
        assert sandboxes[0].name == "my-sandbox"
        assert isinstance(sandboxes[0], Sandbox)

        sandboxes[0]._client.close()


class TestSandboxFromPool:
    """Tests for Sandbox.from_pool()."""

    def test_claim_from_pool(self, mock_env, httpx_mock: HTTPXMock):
        """Test claiming sandbox from pool."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim request
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-abc123", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")

        assert sbx.name == "sandbox-abc123"
        assert sbx.workspace == "test-ws"
        assert sbx.status == "Running"

        # Verify request URL and body
        import json

        requests = httpx_mock.get_requests()
        claim_request = requests[-1]  # Last request is claim
        assert "/sandboxes/claim" in str(claim_request.url)

        # Verify JSON body contains poolName
        body = json.loads(claim_request.content)
        assert body.get("poolName") == "python-pool"

        sbx._client.close()

    def test_claim_from_pool_with_auto_idle_timeout(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """from_pool forwards and stores per-claim auto-idle override."""
        import json

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-abc123", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool", auto_idle_timeout_seconds=900)

        claim_request = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        body = json.loads(claim_request.content)
        assert body["autoIdleTimeoutSeconds"] == 900
        assert sbx.auto_idle_timeout_seconds == 900
        sbx._client.close()


class TestSandboxCreate:
    """Tests for Sandbox.create()."""

    def test_create_sandbox(self, mock_env, httpx_mock: HTTPXMock):
        """Test creating sandbox directly."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock create request
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={"name": "sandbox-xyz789", "status": "Pending"},
        )

        sbx = Sandbox.create(image="python:3.10")

        assert sbx.name == "sandbox-xyz789"
        assert sbx.status == "Pending"

        sbx._client.close()

    def test_create_sandbox_omits_unset_optional_fields(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Unset optional params must not be sent to the backend."""
        import json

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={"name": "sandbox-abc", "status": "Pending"},
        )

        sbx = Sandbox.create(image="python:3.10", name="sandbox-abc")

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
        body = json.loads(post_req.content)
        assert body["image"] == "python:3.10"
        assert body["name"] == "sandbox-abc"
        for key in (
            "cpu",
            "memory",
            "allowInternetAccess",
            "autoIdleTimeoutSeconds",
            "envVars",
            "secretRefs",
        ):
            assert key not in body

        sbx._client.close()

    def test_create_sandbox_with_all_extras(self, mock_env, httpx_mock: HTTPXMock):
        """cpu/memory/allow_internet_access/env_vars/secret_refs should serialize
        to camelCase JSON keys on the wire."""
        import json

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={"name": "sandbox-abc", "status": "Pending"},
        )

        sbx = Sandbox.create(
            image="python:3.10",
            name="sandbox-abc",
            cpu="2",
            memory="4Gi",
            allow_internet_access=True,
            env_vars=[
                {"name": "FOO", "value": "bar"},
                {"name": "HELLO", "value": "world"},
            ],
            secret_refs=["openai-key", "hf-token"],
        )

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
        body = json.loads(post_req.content)
        assert body["image"] == "python:3.10"
        assert body["name"] == "sandbox-abc"
        assert body["cpu"] == "2"
        assert body["memory"] == "4Gi"
        assert body["allowInternetAccess"] is True
        assert "autoIdleTimeoutSeconds" not in body
        assert body["envVars"] == [
            {"name": "FOO", "value": "bar"},
            {"name": "HELLO", "value": "world"},
        ]
        assert body["secretRefs"] == ["openai-key", "hf-token"]
        # snake_case must not leak into the wire format
        assert "allow_internet_access" not in body
        assert "env_vars" not in body
        assert "secret_refs" not in body

        sbx._client.close()

    def test_create_sandbox_with_auto_idle_timeout(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """create forwards and stores per-sandbox auto-idle override."""
        import json

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes",
            json={"name": "sandbox-abc", "status": "Pending"},
        )

        sbx = Sandbox.create(
            image="python:3.10",
            name="sandbox-abc",
            auto_idle_timeout_seconds=1800,
        )

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        body = json.loads(post_req.content)
        assert body["autoIdleTimeoutSeconds"] == 1800
        assert sbx.auto_idle_timeout_seconds == 1800
        sbx._client.close()


class TestSandboxRunCode:
    """Tests for Sandbox.run_code()."""

    def test_run_code(self, mock_env, httpx_mock: HTTPXMock):
        """Test running code in sandbox."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock exec
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "42\n",
                "stderr": "",
                "success": True,
                "execution_time_ms": 50,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.run_code("print(42)")

        assert result.stdout == "42\n"
        assert result.success is True
        assert result.execution_time_ms == 50

        sbx._client.close()

    def test_run_code_timeout_stderr_maps_to_failure(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Timeout-shaped code responses are failures even with success=true."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "Execution timed out after 5 seconds",
                "success": True,
                "durationMs": 5000,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.run_code("while True: pass")

        assert result.success is False
        assert "timed out" in result.stderr
        assert result.execution_time_ms == 5000

        sbx._client.close()

    def test_run_code_timeout_error_name_maps_to_failure(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Structured timeout fields make code execution unsuccessful."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "",
                "success": True,
                "errorName": "TimeoutError",
                "errorValue": "Code execution timed out",
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.run_code("while True: pass")

        assert result.success is False
        assert result.error_name == "TimeoutError"
        assert result.error_value == "Code execution timed out"

        sbx._client.close()

    def test_run_code_empty_camel_error_fields_fall_back_to_snake_case(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Empty camelCase error fields do not hide populated snake_case fields."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "",
                "success": False,
                "errorName": "",
                "error_name": "ValueError",
                "errorValue": None,
                "error_value": "invalid value",
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.run_code("raise ValueError('invalid value')")

        assert result.error_name == "ValueError"
        assert result.error_value == "invalid value"

        sbx._client.close()

    def test_run_code_maintains_session(self, mock_env, httpx_mock: HTTPXMock):
        """Test that session_id is maintained across run_code calls."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # First exec - returns session_id
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "",
                "success": True,
                "execution_time_ms": 50,
                "session_id": "session-abc123",
            },
        )
        # Second exec - should include session_id in request
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "42\n",
                "stderr": "",
                "success": True,
                "execution_time_ms": 30,
                "session_id": "session-abc123",
            },
        )

        sbx = Sandbox.from_pool("python-pool")

        # First call - no session_id yet
        assert sbx.session_id is None
        result1 = sbx.run_code("x = 42")
        assert result1.session_id == "session-abc123"
        assert sbx.session_id == "session-abc123"

        # Second call - should reuse session_id
        result2 = sbx.run_code("print(x)")
        assert result2.stdout == "42\n"

        # Verify second request included session_id
        requests = httpx_mock.get_requests()
        exec_requests = [r for r in requests if "/exec" in str(r.url)]
        assert len(exec_requests) == 2

        # First request should not have session_id
        import json

        first_body = json.loads(exec_requests[0].content)
        assert "session_id" not in first_body or first_body.get("session_id") is None

        # Second request should have session_id
        second_body = json.loads(exec_requests[1].content)
        assert second_body.get("session_id") == "session-abc123"

        sbx._client.close()

    def test_reset_session(self, mock_env, httpx_mock: HTTPXMock):
        """Test that reset_session sets flag for next run_code to reset kernel."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # First exec
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "",
                "success": True,
                "session_id": "session-abc123",
            },
        )
        # Second exec (after reset)
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "",
                "success": True,
                "session_id": "session-new456",
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.run_code("x = 42")
        assert sbx.session_id == "session-abc123"

        # Reset session - clears stale client-side session and sets flag for next exec
        sbx.reset_session()
        assert sbx.session_id is None

        # Next run_code should include reset_session=true in request
        sbx.run_code("y = 1")

        # Verify reset_session was included in the second exec request
        requests = httpx_mock.get_requests()
        exec_requests = [r for r in requests if "/exec" in str(r.url)]
        assert len(exec_requests) == 2
        second_exec_body = exec_requests[1].read().decode()
        import json

        second_body = json.loads(second_exec_body)
        assert second_body.get("reset_session") is True
        assert "session_id" not in second_body

        # After successful call, flag should be cleared
        assert sbx.session_id == "session-new456"

        sbx._client.close()


class TestSandboxCommands:
    """Tests for Sandbox.commands."""

    def test_run_command(self, mock_env, httpx_mock: HTTPXMock):
        """Test running shell command in sandbox."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock exec
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "file1.txt\nfile2.txt\n",
                "stderr": "",
                "exit_code": 0,
                "duration_ms": 100,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.commands.run("ls /workspace")

        assert result.stdout == "file1.txt\nfile2.txt\n"
        assert result.exit_code == 0
        assert result.success is True

        sbx._client.close()

    def test_run_command_timeout_maps_to_non_zero_exit(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Timeout-shaped command responses are not successful exit 0 results."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "[Timeout: no response after 15s]",
                "exitCode": 0,
                "durationMs": 15000,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.commands.run("sleep 30")

        assert result.exit_code == -1
        assert result.success is False
        assert result.stderr == "[Timeout: no response after 15s]"

        sbx._client.close()

    def test_run_command_timeout_error_name_maps_to_non_zero_exit(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Structured timeout fields make command execution unsuccessful."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "",
                "stderr": "",
                "errorName": "ExecutionTimeout",
                "exitCode": 0,
                "durationMs": 15000,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.commands.run("sleep 30")

        assert result.exit_code == -1
        assert result.success is False

        sbx._client.close()

    def test_run_command_ordinary_stderr_timeout_text_stays_success(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Ordinary stderr mentioning timeout is not treated as a timeout result."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "ok\n",
                "stderr": "warning: timeout option ignored\n",
                "exitCode": 0,
                "durationMs": 10,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.commands.run("tool --timeout 5")

        assert result.exit_code == 0
        assert result.success is True
        assert result.stderr == "warning: timeout option ignored\n"

        sbx._client.close()

    def test_run_command_stderr_starting_with_timeout_word_stays_success(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Only backend timeout banners at stderr start are treated as timeouts."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/exec",
            json={
                "stdout": "ok\n",
                "stderr": "timeout option ignored\n",
                "exitCode": 0,
                "durationMs": 10,
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.commands.run("tool --timeout 5")

        assert result.exit_code == 0
        assert result.success is True
        assert result.stderr == "timeout option ignored\n"

        sbx._client.close()


class TestSandboxFiles:
    """Tests for Sandbox.files."""

    def test_write_file(self, mock_env, httpx_mock: HTTPXMock):
        """Test writing file to sandbox."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock file write
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files",
            json={"success": True},
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.files.write("/workspace/test.txt", "hello world")

        # Verify request
        import base64
        import json

        requests = httpx_mock.get_requests()
        file_request = requests[-1]
        assert "/files" in str(file_request.url)

        # The body must include encoding="base64" so the backend knows to
        # decode before forwarding to execd; otherwise the literal base64
        # string ends up on disk (issue #18).
        body = json.loads(file_request.content)
        assert body["encoding"] == "base64"
        assert body["content"] == base64.b64encode(b"hello world").decode("ascii")
        assert body["path"] == "/workspace/test.txt"

        sbx._client.close()

    def test_write_batch_files(self, mock_env, httpx_mock: HTTPXMock):
        """Test writing multiple files with one batch request."""
        import json

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files/batch",
            json={
                "success": True,
                "total": 2,
                "successCount": 2,
                "failureCount": 0,
                "results": [
                    {
                        "index": 0,
                        "path": "/workspace/alpha.txt",
                        "success": True,
                    },
                    {
                        "index": 1,
                        "path": "/workspace/beta.bin",
                        "success": True,
                    },
                ],
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.files.write_batch(
            [
                ("/workspace/alpha.txt", "alpha"),
                ("/workspace/beta.bin", b"\x00\xff"),
            ]
        )

        assert result.success is True
        assert result.success_count == 2
        assert result.failure_count == 0
        assert [item.path for item in result.results] == [
            "/workspace/alpha.txt",
            "/workspace/beta.bin",
        ]

        requests = httpx_mock.get_requests()
        file_request = requests[-1]
        assert "/files/batch" in str(file_request.url)

        body = json.loads(file_request.content)
        assert len(body["items"]) == 2
        assert body["items"][0] == {
            "path": "/workspace/alpha.txt",
            "content": base64.b64encode(b"alpha").decode("ascii"),
            "encoding": "base64",
        }
        assert body["items"][1] == {
            "path": "/workspace/beta.bin",
            "content": base64.b64encode(b"\x00\xff").decode("ascii"),
            "encoding": "base64",
        }

        sbx._client.close()

    def test_write_batch_files_external_api_key_path(
        self, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Batch writes use the external sandbox route for API-key auth."""
        monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
        monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")
        monkeypatch.delenv("PROKUBE_USER_ID", raising=False)
        monkeypatch.setenv("PROKUBE_API_KEY", "secret-key")

        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandboxes/sandbox-test/files/batch",
            json={
                "success": True,
                "total": 1,
                "successCount": 1,
                "failureCount": 0,
                "results": [
                    {
                        "index": 0,
                        "path": "/workspace/alpha.txt",
                        "success": True,
                    }
                ],
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.files.write_batch([("/workspace/alpha.txt", "alpha")])

        assert result.success is True
        requests = httpx_mock.get_requests()
        assert str(requests[-1].url) == (
            "https://test.example.com/sandbox/test-ws/"
            "sandboxes/sandbox-test/files/batch"
        )

        sbx._client.close()

    def test_write_batch_files_partial_failure(self, mock_env, httpx_mock: HTTPXMock):
        """Partial failure responses preserve per-file error details."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files/batch",
            json={
                "success": False,
                "total": 2,
                "successCount": 1,
                "failureCount": 1,
                "results": [
                    {
                        "index": 0,
                        "path": "/workspace/alpha.txt",
                        "success": True,
                    },
                    {
                        "index": 1,
                        "path": "/workspace/beta.txt",
                        "success": False,
                        "error": "Sandbox is not running",
                    },
                ],
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.files.write_batch(
            [
                ("/workspace/alpha.txt", "alpha"),
                ("/workspace/beta.txt", "beta"),
            ]
        )

        assert result.success is False
        assert result.failure_count == 1
        assert result.results[1].error == "Sandbox is not running"

        sbx._client.close()

    def test_write_batch_files_unsupported_backend(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Older backends surface a clear error for missing batch route."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files/batch",
            status_code=404,
            json={"detail": "Not Found"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")

        with pytest.raises(SandboxError, match="require a backend"):
            sbx.files.write_batch([("/workspace/alpha.txt", "alpha")])

        sbx._client.close()

    def test_write_batch_files_rejects_empty_batches(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Empty batches are rejected client-side before any file request."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")

        with pytest.raises(ValueError, match="at least 1 item"):
            sbx.files.write_batch([])

        requests = httpx_mock.get_requests()
        assert all("files/batch" not in str(request.url) for request in requests)

        sbx._client.close()

    def test_write_batch_files_rejects_too_many_items(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Oversized batches are rejected before content is encoded or sent."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")

        with pytest.raises(ValueError, match="at most 100 items"):
            sbx.files.write_batch(
                [(f"/workspace/file-{idx}.txt", "x") for idx in range(101)]
            )

        requests = httpx_mock.get_requests()
        assert all("files/batch" not in str(request.url) for request in requests)

        sbx._client.close()

    def test_read_file(self, mock_env, httpx_mock: HTTPXMock):
        """Test reading file from sandbox."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock file read - uses /files/download?path= endpoint
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files/download?path=%2Fworkspace%2Ftest.txt",
            content=b"hello world",
        )

        sbx = Sandbox.from_pool("python-pool")
        content = sbx.files.read("/workspace/test.txt")

        assert content == b"hello world"

        sbx._client.close()

    def test_list_files(self, mock_env, httpx_mock: HTTPXMock):
        """Test listing files in sandbox."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock file list
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files?path=%2Fworkspace",
            json={
                "files": [
                    {"name": "test.txt", "path": "/workspace/test.txt", "size": 11},
                    {"name": "subdir", "path": "/workspace/subdir", "is_dir": True},
                ]
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        files = sbx.files.list("/workspace")

        assert len(files) == 2
        assert files[0].name == "test.txt"
        assert files[0].size == 11
        assert files[1].is_dir is True

        sbx._client.close()


class TestSandboxKill:
    """Tests for Sandbox.kill()."""

    def test_kill_sandbox(self, mock_env, httpx_mock: HTTPXMock):
        """Test killing sandbox."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock delete
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test",
            status_code=204,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.kill()

        assert sbx.status == "Succeeded"


class TestSandboxContextManager:
    """Tests for Sandbox as context manager."""

    def test_context_manager(self, mock_env, httpx_mock: HTTPXMock):
        """Test using sandbox as context manager."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        # Mock delete
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test",
            status_code=204,
        )

        with Sandbox.from_pool("python-pool") as sbx:
            assert sbx.name == "sandbox-test"

        # Sandbox should be killed after exiting context
        requests = httpx_mock.get_requests()
        delete_request = [r for r in requests if r.method == "DELETE"]
        assert len(delete_request) == 1


class TestSandboxRepr:
    """Tests for Sandbox.__repr__()."""

    def test_repr(self, mock_env, httpx_mock: HTTPXMock):
        """Test string representation."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")
        repr_str = repr(sbx)

        assert "sandbox-test" in repr_str
        assert "test-ws" in repr_str
        assert "Running" in repr_str

        sbx._client.close()
