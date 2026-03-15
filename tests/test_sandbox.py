"""Tests for Sandbox class."""

import pytest
from pytest_httpx import HTTPXMock

from prokube.sandbox import Sandbox


@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")
    monkeypatch.setenv("PROKUBE_USER_ID", "test-user@example.com")


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

        # Verify request body
        requests = httpx_mock.get_requests()
        claim_request = requests[-1]  # Last request is claim
        assert claim_request.url.path == "/api/namespaces/test-ws/sandboxes/claim"

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
        """Test that reset_session clears the session_id."""
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

        sbx = Sandbox.from_pool("python-pool")
        sbx.run_code("x = 42")
        assert sbx.session_id == "session-abc123"

        # Reset session
        sbx.reset_session()
        assert sbx.session_id is None

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
        requests = httpx_mock.get_requests()
        file_request = requests[-1]
        assert "/files" in str(file_request.url)

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
        # Mock file read
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-test/files/workspace/test.txt",
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
