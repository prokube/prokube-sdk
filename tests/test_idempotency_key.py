"""Tests for the Idempotency-Key header on warm-pool claims."""

import uuid

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import PoolExhaustedError
from prokube.sandbox.client import SandboxClient

CLAIM_URL = "https://test.example.com/_platform/sandbox/test-ws/sandboxes/claim"


@pytest.fixture
def config():
    """Create a test config."""
    return Config(
        api_url="https://test.example.com",
        workspace="test-ws",
        user_id="test-user@example.com",
    )


def _mock_version(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://test.example.com/api/version",
        json={"version": "0.1.0"},
    )


def _claim_requests(httpx_mock: HTTPXMock) -> list:
    return [
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and str(r.url) == CLAIM_URL
    ]


class TestClaimIdempotencyKey:
    """The claim endpoint requires an Idempotency-Key header (UUID)."""

    def test_claim_sends_uuid4_idempotency_key(self, config, httpx_mock: HTTPXMock):
        """The header is present and holds a valid version-4 UUID."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            json={"sandboxName": "sandbox-test", "status": "Running"},
        )

        client = SandboxClient(config)
        client.claim_from_pool("python-pool")

        requests = _claim_requests(httpx_mock)
        assert len(requests) == 1
        raw = requests[0].headers.get("Idempotency-Key")
        assert raw is not None
        parsed = uuid.UUID(raw)
        assert parsed.version == 4
        assert str(parsed) == raw
        client.close()

    def test_each_logical_call_uses_a_distinct_key(
        self, config, httpx_mock: HTTPXMock
    ):
        """Two separate claim_from_pool calls are two distinct claims."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            json={"sandboxName": "sandbox-a", "status": "Running"},
            is_reusable=True,
        )

        client = SandboxClient(config)
        client.claim_from_pool("python-pool")
        client.claim_from_pool("python-pool")

        keys = [r.headers["Idempotency-Key"] for r in _claim_requests(httpx_mock)]
        assert len(keys) == 2
        assert keys[0] != keys[1]
        client.close()

    def test_generated_key_reaches_the_wire_unchanged(
        self, config, httpx_mock: HTTPXMock, monkeypatch
    ):
        """The header value is exactly the uuid4 generated for the call."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            json={"sandboxName": "sandbox-test", "status": "Running"},
        )

        fixed = uuid.UUID("11111111-2222-4333-8444-555555555555")
        calls: list[int] = []

        def fake_uuid4() -> uuid.UUID:
            calls.append(1)
            return fixed

        monkeypatch.setattr("prokube.sandbox.client.uuid.uuid4", fake_uuid4)

        client = SandboxClient(config)
        client.claim_from_pool("python-pool", auto_idle_timeout_seconds=900)

        requests = _claim_requests(httpx_mock)
        assert requests[0].headers["Idempotency-Key"] == str(fixed)
        # One key per logical call: uuid4 is consulted exactly once.
        assert len(calls) == 1
        client.close()

    def test_pool_exhausted_still_raises_and_carried_the_header(
        self, config, httpx_mock: HTTPXMock
    ):
        """A 429 pool_exhausted claim keeps its typed error and its key."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            status_code=429,
            headers={"Retry-After": "10"},
            json={"reason": "pool_exhausted"},
        )

        client = SandboxClient(config)
        with pytest.raises(PoolExhaustedError) as exc_info:
            client.claim_from_pool("python-pool")

        assert exc_info.value.status_code == 429
        assert exc_info.value.reason == "pool_exhausted"
        assert exc_info.value.retry_after == "10"

        requests = _claim_requests(httpx_mock)
        assert uuid.UUID(requests[0].headers["Idempotency-Key"]).version == 4
        client.close()

    def test_auth_headers_survive_the_idempotency_header(
        self, config, httpx_mock: HTTPXMock
    ):
        """Per-request headers merge over, never replace, client defaults."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            json={"sandboxName": "sandbox-test", "status": "Running"},
        )

        client = SandboxClient(config)
        client.claim_from_pool("python-pool")

        request = _claim_requests(httpx_mock)[0]
        assert request.headers["kubeflow-userid"] == "test-user@example.com"
        assert "Idempotency-Key" in request.headers
        client.close()
