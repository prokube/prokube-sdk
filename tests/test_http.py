"""Tests for HTTP client."""

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import ProKubeError, SandboxNotFoundError
from prokube.common.http import HttpClient


@pytest.fixture
def config():
    """Create a test config."""
    return Config(
        api_url="https://test.example.com",
        workspace="test-ws",
        user_id="test-user@example.com",
    )


@pytest.fixture
def http_client(config):
    """Create an HTTP client for testing."""
    client = HttpClient(config)
    yield client
    client.close()


class TestHttpClient:
    """Tests for HttpClient class."""

    def test_get_request(self, http_client: HttpClient, httpx_mock: HTTPXMock):
        """Test successful GET request."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/test",
            json={"result": "success"},
        )

        response = http_client.get("/api/test")
        assert response == {"result": "success"}

    def test_post_request(self, http_client: HttpClient, httpx_mock: HTTPXMock):
        """Test successful POST request."""
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/test",
            json={"created": True},
        )

        response = http_client.post("/api/test", json={"data": "value"})
        assert response == {"created": True}

    def test_delete_request(self, http_client: HttpClient, httpx_mock: HTTPXMock):
        """Test successful DELETE request."""
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/test",
            status_code=204,
        )

        response = http_client.delete("/api/test")
        assert response is None

    def test_get_bytes(self, http_client: HttpClient, httpx_mock: HTTPXMock):
        """Test GET request returning bytes."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/file",
            content=b"binary content",
        )

        response = http_client.get_bytes("/api/file")
        assert response == b"binary content"

    def test_404_raises_sandbox_not_found(
        self, http_client: HttpClient, httpx_mock: HTTPXMock
    ):
        """Test that 404 raises SandboxNotFoundError."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/sandbox/missing",
            status_code=404,
        )

        with pytest.raises(SandboxNotFoundError, match="not found"):
            http_client.get("/api/sandbox/missing")

    def test_500_raises_prokube_error(
        self, http_client: HttpClient, httpx_mock: HTTPXMock
    ):
        """Test that 500 raises ProKubeError."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/error",
            status_code=500,
            json={"detail": "Internal server error"},
        )

        with pytest.raises(ProKubeError, match="Internal server error"):
            http_client.get("/api/error")

    def test_auth_headers_included(
        self, http_client: HttpClient, httpx_mock: HTTPXMock
    ):
        """Test that auth headers are included in requests."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/test",
            json={},
        )

        http_client.get("/api/test")

        request = httpx_mock.get_request()
        assert request.headers["kubeflow-userid"] == "test-user@example.com"
