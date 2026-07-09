"""Tests for SandboxClient edge cases."""

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import PoolExhaustedError
from prokube.sandbox.client import SandboxClient, _parse_status
from prokube.sandbox.models import SandboxStatus


@pytest.fixture
def config():
    """Create a test config."""
    return Config(
        api_url="https://test.example.com",
        workspace="test-ws",
        user_id="test-user@example.com",
    )


class TestParseStatus:
    """Tests for _parse_status helper function."""

    def test_valid_status(self):
        """Test parsing valid status strings."""
        assert _parse_status("Running", SandboxStatus.UNKNOWN) == SandboxStatus.RUNNING
        assert _parse_status("Pending", SandboxStatus.UNKNOWN) == SandboxStatus.PENDING
        assert (
            _parse_status("Succeeded", SandboxStatus.UNKNOWN) == SandboxStatus.SUCCEEDED
        )
        assert _parse_status("Failed", SandboxStatus.UNKNOWN) == SandboxStatus.FAILED

    def test_unknown_status_falls_back(self):
        """Test that unknown status values fall back to UNKNOWN."""
        assert (
            _parse_status("SomeNewStatus", SandboxStatus.RUNNING)
            == SandboxStatus.UNKNOWN
        )
        assert _parse_status("invalid", SandboxStatus.PENDING) == SandboxStatus.UNKNOWN

    def test_none_uses_default(self):
        """Test that None uses the provided default."""
        assert _parse_status(None, SandboxStatus.RUNNING) == SandboxStatus.RUNNING
        assert _parse_status(None, SandboxStatus.PENDING) == SandboxStatus.PENDING

    def test_empty_string_uses_default(self):
        """Test that empty string uses the provided default."""
        assert _parse_status("", SandboxStatus.RUNNING) == SandboxStatus.RUNNING


class TestListSandboxes:
    """Tests for SandboxClient.list()."""

    def test_list_empty(self, config, httpx_mock: HTTPXMock):
        """Test listing sandboxes when none exist."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes",
            json={"sandboxes": [], "total": 0},
        )

        client = SandboxClient(config)
        result = client.list()

        assert result == []
        client.close()

    def test_list_multiple(self, config, httpx_mock: HTTPXMock):
        """Test listing multiple sandboxes."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {
                        "name": "sandbox-1",
                        "namespace": "test-ws",
                        "image": "python:3.10",
                        "phase": "Running",
                        "poolName": "python-pool",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "auto_idle_timeout_seconds": 900,
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

        client = SandboxClient(config)
        result = client.list()

        assert len(result) == 2
        assert result[0].name == "sandbox-1"
        assert result[0].status == SandboxStatus.RUNNING
        assert result[0].image == "python:3.10"
        assert result[0].pool == "python-pool"
        assert result[0].created_at == "2026-01-01T00:00:00Z"
        assert result[0].auto_idle_timeout_seconds == 900
        assert result[1].name == "sandbox-2"
        assert result[1].status == SandboxStatus.PENDING
        assert result[1].pool is None
        client.close()

    def test_claim_sends_auto_idle_timeout(self, config, httpx_mock: HTTPXMock):
        """claim_from_pool sends per-claim auto-idle override."""
        import json

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )

        client = SandboxClient(config)
        info = client.claim_from_pool("python-pool", auto_idle_timeout_seconds=900)

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        body = json.loads(post_req.content)
        assert body["poolName"] == "python-pool"
        assert body["autoIdleTimeoutSeconds"] == 900
        assert info.auto_idle_timeout_seconds == 900
        client.close()

    def test_claim_pool_exhausted_raises_retryable_error(
        self, config, httpx_mock: HTTPXMock
    ):
        """claim_from_pool exposes 429 pool_exhausted distinctly."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes/claim",
            status_code=429,
            headers={"Retry-After": "10"},
            json={"error": "pool_exhausted"},
        )

        client = SandboxClient(config)
        with pytest.raises(PoolExhaustedError) as exc_info:
            client.claim_from_pool("python-pool")

        assert exc_info.value.status_code == 429
        assert exc_info.value.reason == "pool_exhausted"
        assert exc_info.value.retry_after == "10"
        client.close()

    def test_list_with_status_field(self, config, httpx_mock: HTTPXMock):
        """Test listing sandboxes when backend returns 'status' instead of 'phase'."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {
                        "name": "sandbox-1",
                        "namespace": "test-ws",
                        "status": "Running",
                    },
                ],
                "total": 1,
            },
        )

        client = SandboxClient(config)
        result = client.list()

        assert len(result) == 1
        assert result[0].status == SandboxStatus.RUNNING
        client.close()


class TestReadFile:
    """Tests for read_file endpoint."""

    def test_read_file_with_spaces(self, config, httpx_mock: HTTPXMock):
        """Test reading file with spaces in path uses query param."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock file download - path is passed as query parameter
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes/test-sbx/files/download?path=%2Fworkspace%2Fmy+file.txt",
            content=b"content",
        )

        client = SandboxClient(config)
        content = client.read_file("test-sbx", "/workspace/my file.txt")

        assert content == b"content"
        client.close()

    def test_read_file_with_special_chars(self, config, httpx_mock: HTTPXMock):
        """Test reading file with special characters in path."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock file download - special chars in query param
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes/test-sbx/files/download?path=%2Fworkspace%2Ffile%23%3F.txt",
            content=b"content",
        )

        client = SandboxClient(config)
        content = client.read_file("test-sbx", "/workspace/file#?.txt")

        assert content == b"content"
        client.close()


class TestUnknownStatusFromBackend:
    """Tests for handling unknown status from backend."""

    def test_claim_with_unknown_status(self, config, httpx_mock: HTTPXMock):
        """Test that unknown status from backend doesn't crash."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock claim with unknown status
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/_platform/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "SomeNewBackendStatus"},
        )

        client = SandboxClient(config)
        info = client.claim_from_pool("python-pool")

        # Should fall back to UNKNOWN instead of crashing
        assert info.status == SandboxStatus.UNKNOWN
        assert info.name == "sandbox-test"
        client.close()
