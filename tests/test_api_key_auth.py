"""Tests for API key authentication and external access routing."""

import os
from unittest.mock import patch

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.auth import get_auth_headers
from prokube.common.config import Config
from prokube.common.exceptions import AuthenticationError
from prokube.sandbox import Sandbox
from prokube.sandbox.client import SandboxClient

_AUTH_ENV_VARS = ("PROKUBE_API_KEY", "PROKUBE_USER_ID", "KF_USER")


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    """Ensure auth env vars don't leak into tests from the developer's shell."""
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestConfigApiKey:
    """Tests for Config with api_key field."""

    def test_config_with_api_key(self):
        """Test creating config with explicit api_key."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            api_key="test-key-123",
        )
        assert config.api_key == "test-key-123"
        assert config.use_api_key is True

    def test_config_with_user_id_only(self):
        """Test config with user_id has use_api_key False."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
        )
        assert config.api_key is None
        assert config.use_api_key is False

    def test_config_api_key_from_env(self):
        """Test that PROKUBE_API_KEY env var is picked up."""
        env = {
            "PROKUBE_API_URL": "https://example.com",
            "PROKUBE_WORKSPACE": "test-ws",
            "PROKUBE_API_KEY": "env-key-456",
        }
        with patch.dict(os.environ, env, clear=False):
            config = Config()
            assert config.api_key == "env-key-456"
            assert config.use_api_key is True

    def test_config_empty_api_key_env_is_none(self):
        """Test that empty PROKUBE_API_KEY is treated as None."""
        env = {
            "PROKUBE_API_URL": "https://example.com",
            "PROKUBE_WORKSPACE": "test-ws",
            "PROKUBE_API_KEY": "",
            "PROKUBE_USER_ID": "user@test.com",
        }
        with patch.dict(os.environ, env, clear=False):
            config = Config()
            assert config.api_key is None
            assert config.use_api_key is False

    def test_config_both_api_key_and_user_id(self):
        """Test that both can be set (api_key takes precedence in auth)."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
            api_key="my-key",
        )
        assert config.api_key == "my-key"
        assert config.user_id == "user@test.com"
        assert config.use_api_key is True


class TestAuthHeaders:
    """Tests for auth header generation."""

    def test_api_key_header(self):
        """Test that api_key produces x-api-key header."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            api_key="my-key",
        )
        headers = get_auth_headers(config)
        assert headers == {"x-api-key": "my-key"}

    def test_user_id_header(self):
        """Test that user_id produces kubeflow-userid header."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
        )
        headers = get_auth_headers(config)
        assert headers == {"kubeflow-userid": "user@test.com"}

    def test_api_key_takes_precedence(self):
        """Test that api_key takes precedence over user_id."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
            api_key="my-key",
        )
        headers = get_auth_headers(config)
        assert headers == {"x-api-key": "my-key"}

    def test_no_credentials_raises(self):
        """Test that missing both api_key and user_id raises."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
        )
        with pytest.raises(AuthenticationError, match="No api_key or user_id"):
            get_auth_headers(config)


class TestPathRouting:
    """Tests for internal vs external path routing in SandboxClient."""

    def test_internal_sandboxes_path(self):
        """Test internal (user_id) sandboxes path."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
        )
        client = SandboxClient(config, check_version=False)
        assert client._sandboxes_path() == "/api/namespaces/test-ws/sandboxes"
        client.close()

    def test_external_sandboxes_path(self):
        """Test external (api_key) sandboxes path."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            api_key="my-key",
        )
        client = SandboxClient(config, check_version=False)
        assert client._sandboxes_path() == "/sandbox/test-ws/sandboxes"
        client.close()

    def test_internal_sandbox_path(self):
        """Test internal sandbox path for a specific sandbox."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
        )
        client = SandboxClient(config, check_version=False)
        assert (
            client._sandbox_path("my-sbx")
            == "/api/namespaces/test-ws/sandboxes/my-sbx"
        )
        client.close()

    def test_external_sandbox_path(self):
        """Test external sandbox path for a specific sandbox."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            api_key="my-key",
        )
        client = SandboxClient(config, check_version=False)
        assert client._sandbox_path("my-sbx") == "/sandbox/test-ws/sandboxes/my-sbx"
        client.close()

    def test_internal_sub_path(self):
        """Test internal sub-resource paths (exec, files)."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            user_id="user@test.com",
        )
        client = SandboxClient(config, check_version=False)
        assert (
            client._sandbox_sub_path("my-sbx", "exec")
            == "/api/namespaces/test-ws/sandboxes/my-sbx/exec"
        )
        assert (
            client._sandbox_sub_path("my-sbx", "files")
            == "/api/namespaces/test-ws/sandboxes/my-sbx/files"
        )
        assert (
            client._sandbox_sub_path("my-sbx", "files/download")
            == "/api/namespaces/test-ws/sandboxes/my-sbx/files/download"
        )
        client.close()

    def test_external_sub_path(self):
        """Test external sub-resource paths (exec, files)."""
        config = Config(
            api_url="https://example.com",
            workspace="test-ws",
            api_key="my-key",
        )
        client = SandboxClient(config, check_version=False)
        assert (
            client._sandbox_sub_path("my-sbx", "exec")
            == "/sandbox/test-ws/my-sbx/exec"
        )
        assert (
            client._sandbox_sub_path("my-sbx", "files")
            == "/sandbox/test-ws/my-sbx/files"
        )
        assert (
            client._sandbox_sub_path("my-sbx", "files/download")
            == "/sandbox/test-ws/my-sbx/files/download"
        )
        client.close()


class TestApiKeyEndToEnd:
    """End-to-end tests using API key auth with mocked HTTP."""

    @pytest.fixture
    def mock_env(self, monkeypatch):
        """Set up environment variables for API key testing."""
        monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
        monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")
        monkeypatch.setenv("PROKUBE_API_KEY", "test-api-key")

    def test_from_pool_with_api_key(self, mock_env, httpx_mock: HTTPXMock):
        """Test claiming from pool uses external paths with API key."""
        # No version check expected with API key
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-abc", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")

        assert sbx.name == "sandbox-abc"
        assert sbx.status == "Running"

        # Verify x-api-key header was used
        requests = httpx_mock.get_requests()
        assert len(requests) == 1  # No version check
        assert requests[0].headers["x-api-key"] == "test-api-key"

        sbx._client.close()

    def test_list_with_api_key(self, mock_env, httpx_mock: HTTPXMock):
        """Test listing sandboxes uses external paths with API key."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/sandbox/test-ws/sandboxes",
            json={"sandboxes": [{"name": "sbx-1", "status": "Running"}], "total": 1},
        )

        sandboxes = Sandbox.list()

        assert len(sandboxes) == 1
        assert sandboxes[0].name == "sbx-1"

        for sbx in sandboxes:
            sbx._client.close()

    def test_exec_with_api_key(self, mock_env, httpx_mock: HTTPXMock):
        """Test exec uses external sub-resource path with API key."""
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandbox-test/exec",
            json={"stdout": "42\n", "stderr": "", "success": True},
        )

        sbx = Sandbox.from_pool("python-pool")
        result = sbx.run_code("print(42)")

        assert result.stdout == "42\n"
        sbx._client.close()

    def test_version_check_skipped_with_api_key(self, mock_env, httpx_mock: HTTPXMock):
        """Test that version check is skipped when using API key."""
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")

        # Only the claim request should have been made (no /api/version)
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert "/api/version" not in str(requests[0].url)

        sbx._client.close()

    def test_explicit_api_key_param(self, monkeypatch, httpx_mock: HTTPXMock):
        """Test passing api_key explicitly to Sandbox methods."""
        monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
        monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")

        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/test-ws/sandboxes/claim",
            json={"name": "sandbox-test", "status": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool", api_key="explicit-key")

        requests = httpx_mock.get_requests()
        assert requests[0].headers["x-api-key"] == "explicit-key"

        sbx._client.close()
