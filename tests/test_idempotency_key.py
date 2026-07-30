"""Tests for the Idempotency-Key header on warm-pool claims."""

import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import PoolExhaustedError, ProKubeError
from prokube.sandbox.client import (
    _CLAIM_POOL_EXHAUSTED_BUDGET_SECONDS,
    SandboxClient,
)
from prokube.sandbox.models import SandboxStatus

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


def _claim_keys(httpx_mock: HTTPXMock) -> list[str]:
    return [r.headers["Idempotency-Key"] for r in _claim_requests(httpx_mock)]


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Capture (and skip) the retry backoff so the suite stays fast.

    The pool-exhausted retry budget is wall-clock bounded, so the fake clock
    advances exactly as far as each skipped sleep. Budget assertions are then
    deterministic without the suite ever really sleeping.
    """
    recorded: list[float] = []
    now = [1000.0]

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("prokube.sandbox.client.time.sleep", fake_sleep)
    monkeypatch.setattr("prokube.sandbox.client.time.monotonic", lambda: now[0])
    return recorded


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
        """A 429 pool_exhausted claim keeps its typed error and its key.

        A zero retry budget makes the first 429 terminal, which isolates the
        error shape from the retry behaviour covered below.
        """
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
            client.claim_from_pool("python-pool", pool_exhausted_budget_seconds=0)

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


class TestClaimTransportRetry:
    """Transport failures are replayed under the same key; HTTP results are not."""

    def test_lost_response_is_recovered_under_the_same_key(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """A dropped connection is retried and the claim still succeeds."""
        _mock_version(httpx_mock)
        httpx_mock.add_exception(
            httpx.ConnectError("connection lost"), method="POST", url=CLAIM_URL
        )
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            status_code=201,
            json={"sandboxName": "sandbox-claimed", "status": "Running"},
        )

        client = SandboxClient(config)
        info = client.claim_from_pool("python-pool")

        assert info.name == "sandbox-claimed"
        keys = _claim_keys(httpx_mock)
        # The retry is a replay, not a second claim: one key, two attempts.
        assert len(keys) == 2
        assert keys[0] == keys[1]
        assert uuid.UUID(keys[0]).version == 4
        assert sleeps == [0.5]
        client.close()

    def test_exhausted_attempts_reraise_the_transport_error(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """Three failed attempts surface the transport error unchanged."""
        _mock_version(httpx_mock)
        for _ in range(3):
            httpx_mock.add_exception(
                httpx.ConnectError("connection lost"), method="POST", url=CLAIM_URL
            )

        client = SandboxClient(config)
        with pytest.raises(httpx.ConnectError):
            client.claim_from_pool("python-pool")

        keys = _claim_keys(httpx_mock)
        assert len(keys) == 3
        assert len(set(keys)) == 1
        # Bounded: backoff runs between attempts only, never after the last.
        assert sleeps == [0.5, 1.0]
        client.close()

    def test_429_without_a_structured_reason_is_not_retried(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """Only ``pool_exhausted`` is retryable; a bare 429 stays terminal."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            status_code=429,
            headers={"Retry-After": "10"},
            json={"detail": "slow down"},
        )

        client = SandboxClient(config)
        with pytest.raises(ProKubeError) as exc_info:
            client.claim_from_pool("python-pool")

        assert not isinstance(exc_info.value, PoolExhaustedError)
        assert exc_info.value.status_code == 429
        assert len(_claim_requests(httpx_mock)) == 1
        assert sleeps == []
        client.close()

    def test_server_error_is_not_retried(self, config, httpx_mock: HTTPXMock, sleeps):
        """A 500 produced a response, so replaying it would only hide it."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            status_code=500,
            json={"detail": "boom"},
        )

        client = SandboxClient(config)
        with pytest.raises(ProKubeError) as exc_info:
            client.claim_from_pool("python-pool")

        assert exc_info.value.status_code == 500
        assert len(_claim_requests(httpx_mock)) == 1
        assert sleeps == []
        client.close()

    def test_retries_do_not_bleed_keys_across_logical_calls(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """A retried call and a fresh call are still two distinct claims."""
        _mock_version(httpx_mock)
        httpx_mock.add_exception(
            httpx.ConnectError("connection lost"), method="POST", url=CLAIM_URL
        )
        httpx_mock.add_response(
            method="POST",
            url=CLAIM_URL,
            json={"sandboxName": "sandbox-a", "status": "Running"},
            is_reusable=True,
        )

        client = SandboxClient(config)
        client.claim_from_pool("python-pool")
        client.claim_from_pool("python-pool")

        keys = _claim_keys(httpx_mock)
        assert len(keys) == 3
        # Attempts 1-2 replay one claim; the second logical call is a new one.
        assert keys[0] == keys[1]
        assert keys[2] != keys[0]
        client.close()


def _exhausted(httpx_mock: HTTPXMock, retry_after: str | None = "10", **kwargs) -> None:
    """Register a 429 pool_exhausted claim response."""
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    httpx_mock.add_response(
        method="POST",
        url=CLAIM_URL,
        status_code=429,
        headers=headers,
        json={"reason": "pool_exhausted"},
        **kwargs,
    )


def _claimed(httpx_mock: HTTPXMock, name: str = "sandbox-claimed") -> None:
    """Register a successful claim response."""
    httpx_mock.add_response(
        method="POST",
        url=CLAIM_URL,
        status_code=201,
        json={"sandboxName": name, "status": "Running"},
    )


class TestClaimPoolExhaustedRetry:
    """A 429 pool_exhausted claim is replayed under the same key, within budget."""

    def test_transient_exhaustion_is_replayed_under_the_same_key(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """429, 429, 201: the caller sees only the sandbox it asked for."""
        _mock_version(httpx_mock)
        _exhausted(httpx_mock)
        _exhausted(httpx_mock)
        _claimed(httpx_mock)

        client = SandboxClient(config)
        info = client.claim_from_pool("python-pool")

        assert info.name == "sandbox-claimed"
        assert info.status is SandboxStatus.RUNNING
        keys = _claim_keys(httpx_mock)
        # One logical claim: the 429 replays carry the key too, so the backend
        # can coalesce them instead of handing out a second sandbox.
        assert len(keys) == 3
        assert len(set(keys)) == 1
        assert uuid.UUID(keys[0]).version == 4
        # Retry-After wins over the linear backoff.
        assert sleeps == [10.0, 10.0]
        client.close()

    def test_missing_retry_after_falls_back_to_the_linear_backoff(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """Without a hint the claim reuses the transport-retry backoff."""
        _mock_version(httpx_mock)
        _exhausted(httpx_mock, retry_after=None)
        _exhausted(httpx_mock, retry_after="not-a-delay")
        _exhausted(httpx_mock, retry_after="0")
        _claimed(httpx_mock)

        client = SandboxClient(config)
        client.claim_from_pool("python-pool")

        # Unparseable and non-positive hints are ignored, not obeyed as 0.
        assert sleeps == [0.5, 1.0, 1.5]
        assert len(set(_claim_keys(httpx_mock))) == 1
        client.close()

    def test_http_date_retry_after_is_honored(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """RFC 9110 allows an HTTP-date hint; it is converted to a delay."""
        _mock_version(httpx_mock)
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        _exhausted(httpx_mock, retry_after=format_datetime(when, usegmt=True))
        _claimed(httpx_mock)

        client = SandboxClient(config)
        client.claim_from_pool("python-pool")

        assert len(sleeps) == 1
        assert 25 <= sleeps[0] <= 30
        client.close()

    def test_budget_exhaustion_raises_pool_exhausted_unchanged(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """A pool that stays dry surfaces the typed error once the budget ends."""
        _mock_version(httpx_mock)
        _exhausted(httpx_mock, is_reusable=True)

        client = SandboxClient(config)
        with pytest.raises(PoolExhaustedError) as exc_info:
            client.claim_from_pool("python-pool", pool_exhausted_budget_seconds=15)

        # The error shape is the backend's, untouched by the retry loop.
        assert exc_info.value.status_code == 429
        assert exc_info.value.reason == "pool_exhausted"
        assert exc_info.value.retry_after == "10"
        # 15s of budget absorbs one 10s wait, never a second one.
        assert sleeps == [10.0]
        assert len(_claim_requests(httpx_mock)) == 2
        assert len(set(_claim_keys(httpx_mock))) == 1
        client.close()

    def test_a_hint_larger_than_the_budget_is_refused_immediately(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """Budget is a bound on the wait, not something to truncate a sleep to."""
        _mock_version(httpx_mock)
        _exhausted(httpx_mock, retry_after="600")

        client = SandboxClient(config)
        with pytest.raises(PoolExhaustedError):
            client.claim_from_pool("python-pool")

        assert sleeps == []
        assert len(_claim_requests(httpx_mock)) == 1
        client.close()

    def test_default_budget_is_generous(self, config, httpx_mock: HTTPXMock, sleeps):
        """The module default keeps retrying for about a minute."""
        _mock_version(httpx_mock)
        _exhausted(httpx_mock, retry_after="5", is_reusable=True)

        client = SandboxClient(config)
        with pytest.raises(PoolExhaustedError):
            client.claim_from_pool("python-pool")

        assert _CLAIM_POOL_EXHAUSTED_BUDGET_SECONDS >= 60
        assert sum(sleeps) == pytest.approx(_CLAIM_POOL_EXHAUSTED_BUDGET_SECONDS)
        assert len(set(_claim_keys(httpx_mock))) == 1
        client.close()

    def test_transport_and_exhaustion_share_one_key(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """Mixed failures are still a single logical claim."""
        _mock_version(httpx_mock)
        httpx_mock.add_exception(
            httpx.ConnectError("connection lost"), method="POST", url=CLAIM_URL
        )
        _exhausted(httpx_mock)
        _claimed(httpx_mock)

        client = SandboxClient(config)
        info = client.claim_from_pool("python-pool")

        assert info.name == "sandbox-claimed"
        keys = _claim_keys(httpx_mock)
        assert len(keys) == 3
        assert len(set(keys)) == 1
        assert sleeps == [0.5, 10.0]
        client.close()

    def test_each_logical_call_still_gets_its_own_key(
        self, config, httpx_mock: HTTPXMock, sleeps
    ):
        """A retried claim does not bleed its key into the next call."""
        _mock_version(httpx_mock)
        _exhausted(httpx_mock)
        _claimed(httpx_mock, name="sandbox-a")
        _claimed(httpx_mock, name="sandbox-b")

        client = SandboxClient(config)
        first = client.claim_from_pool("python-pool")
        second = client.claim_from_pool("python-pool")

        assert (first.name, second.name) == ("sandbox-a", "sandbox-b")
        keys = _claim_keys(httpx_mock)
        assert keys[0] == keys[1]
        assert keys[2] != keys[0]
        client.close()
