"""Tests for configuration module."""

import os
from unittest.mock import patch

import pytest

from prokube.common.config import Config


class TestConfig:
    """Tests for Config class."""

    def test_config_from_explicit_params(self):
        """Test creating config with explicit parameters."""
        config = Config(
            api_url="https://example.com/pkui",
            workspace="test-ws",
            user_id="user@test.com",
            timeout=600,
        )
        assert config.api_url == "https://example.com/pkui"
        assert config.workspace == "test-ws"
        assert config.user_id == "user@test.com"
        assert config.timeout == 600

    def test_config_strips_trailing_slash(self):
        """Test that trailing slash is removed from API URL."""
        config = Config(
            api_url="https://example.com/pkui/",
            workspace="test-ws",
        )
        assert config.api_url == "https://example.com/pkui"

    def test_config_from_env_vars(self):
        """Test creating config from environment variables."""
        env = {
            "PROKUBE_API_URL": "https://env.example.com",
            "PROKUBE_WORKSPACE": "env-ws",
            "PROKUBE_USER_ID": "env-user@test.com",
            "PROKUBE_TIMEOUT": "120",
        }
        with patch.dict(os.environ, env, clear=False):
            config = Config()
            assert config.api_url == "https://env.example.com"
            assert config.workspace == "env-ws"
            assert config.user_id == "env-user@test.com"
            assert config.timeout == 120

    def test_config_kf_user_fallback(self):
        """Test that KF_USER is used as fallback for user_id."""
        env = {
            "PROKUBE_API_URL": "https://example.com",
            "PROKUBE_WORKSPACE": "test-ws",
            "KF_USER": "kubeflow-user@test.com",
        }
        # Clear PROKUBE_USER_ID if it exists
        with patch.dict(os.environ, env, clear=False):
            with patch.dict(os.environ, {"PROKUBE_USER_ID": ""}, clear=False):
                os.environ.pop("PROKUBE_USER_ID", None)
                config = Config()
                assert config.user_id == "kubeflow-user@test.com"

    def test_config_defaults_to_internal_agent_gateway_url(self):
        """Test that missing API URL defaults to in-cluster Agent Gateway."""
        with patch.dict(
            os.environ, {"KUBERNETES_SERVICE_HOST": "10.152.0.1"}, clear=True
        ):
            config = Config(workspace="test-ws")
            assert (
                config.api_url
                == "http://agentgateway-proxy.agentgateway-system.svc.cluster.local"
            )

    def test_config_missing_api_url_outside_cluster_raises(self):
        """Outside Kubernetes, missing API URL should fail clearly."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="API URL is required"):
                Config(workspace="test-ws")

    def test_config_with_api_key_requires_api_url(self):
        """External API key access should fail clearly without an API URL."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="API URL is required"):
                Config(workspace="test-ws", api_key="test-key")

    def test_config_missing_workspace_raises(self):
        """Test that missing workspace raises ValueError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="Workspace is required"):
                Config(api_url="https://example.com")

    def test_config_invalid_timeout_uses_default(self):
        """Test that invalid timeout falls back to default."""
        env = {
            "PROKUBE_API_URL": "https://example.com",
            "PROKUBE_WORKSPACE": "test-ws",
            "PROKUBE_TIMEOUT": "invalid",
        }
        with patch.dict(os.environ, env, clear=False):
            config = Config()
            assert config.timeout == 300  # default
