"""Tests for HTTP client."""

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import NotFoundError, PoolExhaustedError, ProKubeError
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

    def test_404_raises_not_found_error(
        self, http_client: HttpClient, httpx_mock: HTTPXMock
    ):
        """Test that 404 raises NotFoundError."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/sandbox/missing",
            status_code=404,
        )

        with pytest.raises(NotFoundError, match="not found"):
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

    def test_429_pool_exhausted_raises_pool_exhausted_error(
        self, http_client: HttpClient, httpx_mock: HTTPXMock
    ):
        """Structured pool_exhausted responses are retryable typed errors."""
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/sandboxes/claim",
            status_code=429,
            headers={"Retry-After": "5"},
            json={"reason": "pool_exhausted", "error": "pool_exhausted"},
        )

        with pytest.raises(PoolExhaustedError) as exc_info:
            http_client.post("/api/sandboxes/claim", json={"poolName": "python-pool"})

        assert exc_info.value.status_code == 429
        assert exc_info.value.reason == "pool_exhausted"
        assert exc_info.value.retry_after == "5"
        assert "No warm pool capacity" in str(exc_info.value)

    def test_detail_429_pool_exhausted_raises_pool_exhausted_error(
        self, http_client: HttpClient, httpx_mock: HTTPXMock
    ):
        """The backend may nest the structured reason under detail."""
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/sandboxes/claim",
            status_code=429,
            json={"detail": {"reason": "pool_exhausted"}},
        )

        with pytest.raises(PoolExhaustedError) as exc_info:
            http_client.post("/api/sandboxes/claim", json={"poolName": "python-pool"})

        assert exc_info.value.retry_after is None

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


class TestGetOrigin:
    """Tests for HttpClient._get_origin."""

    def test_simple_url(self):
        assert HttpClient._get_origin("https://example.com") == "https://example.com"

    def test_url_with_path(self):
        assert (
            HttpClient._get_origin("https://example.com/pkui") == "https://example.com"
        )

    def test_url_with_port(self):
        assert (
            HttpClient._get_origin("https://example.com:8080/pkui")
            == "https://example.com:8080"
        )

    def test_url_with_deep_path(self):
        assert (
            HttpClient._get_origin("https://example.com/a/b/c") == "https://example.com"
        )

    def test_missing_scheme_raises(self):
        with pytest.raises(ValueError, match="absolute URL including scheme"):
            HttpClient._get_origin("example.com/pkui")

    def test_non_http_scheme_raises(self):
        with pytest.raises(ValueError, match="http or https scheme"):
            HttpClient._get_origin("ftp://example.com/pkui")

    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="absolute URL including scheme"):
            HttpClient._get_origin("")


class TestApiKeyBaseUrl:
    """Tests that API key auth uses origin-only base URL."""

    def test_api_key_strips_path_prefix(self, httpx_mock: HTTPXMock):
        """API key client should use origin only, not the /pkui path prefix."""
        config = Config(
            api_url="https://test.example.com/pkui",
            workspace="test-ws",
            api_key="pk_live_test123",
        )
        client = HttpClient(config)

        # External route: /sandbox/test-ws/sandboxes (no /pkui prefix)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/sandbox/test-ws/sandboxes",
            json={"sandboxes": [], "total": 0},
        )

        response = client.get("/sandbox/test-ws/sandboxes")
        assert response == {"sandboxes": [], "total": 0}

        request = httpx_mock.get_request()
        assert request.headers["x-api-key"] == "pk_live_test123"
        assert str(request.url) == "https://test.example.com/sandbox/test-ws/sandboxes"
        client.close()

    def test_internal_uses_full_path(self, httpx_mock: HTTPXMock):
        """Internal client should preserve the /pkui path prefix."""
        config = Config(
            api_url="https://test.example.com/pkui",
            workspace="test-ws",
            user_id="test-user@example.com",
        )
        client = HttpClient(config)

        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/pkui/_platform/sandbox/test-ws/sandboxes",
            json={"sandboxes": [], "total": 0},
        )

        response = client.get("/_platform/sandbox/test-ws/sandboxes")
        assert response == {"sandboxes": [], "total": 0}

        request = httpx_mock.get_request()
        assert (
            str(request.url)
            == "https://test.example.com/pkui/_platform/sandbox/test-ws/sandboxes"
        )
        client.close()
