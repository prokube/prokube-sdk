"""Tests for SandboxClient edge cases."""

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
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


class TestReadFileUrlEncoding:
    """Tests for URL encoding in read_file."""

    def test_read_file_with_spaces(self, config, httpx_mock: HTTPXMock):
        """Test reading file with spaces in path."""
        # Mock version check
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        # Mock file read - note the URL-encoded space (%20)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/test-sbx/files/workspace/my%20file.txt",
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
        # Mock file read - special chars encoded
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/test-sbx/files/workspace/file%23%3F.txt",
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
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "SomeNewBackendStatus"},
        )

        client = SandboxClient(config)
        info = client.claim_from_pool("python-pool")

        # Should fall back to UNKNOWN instead of crashing
        assert info.status == SandboxStatus.UNKNOWN
        assert info.name == "sandbox-test"
        client.close()
