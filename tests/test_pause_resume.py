"""Tests for pause/resume functionality."""

import json
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError, SandboxTimeoutError
from prokube.sandbox import Sandbox
from prokube.sandbox.client import SandboxClient, _parse_status
from prokube.sandbox.models import SandboxStatus

_WARMUP_MARKER_RE = re.compile(r'print\("(__pk_warmup_[0-9a-f]+__)"\)')


def _extract_marker(request: httpx.Request) -> str | None:
    """Extract the warmup marker from an /exec request body, if present."""
    try:
        body = json.loads(request.content)
    except ValueError:
        # ValueError covers both json.JSONDecodeError (bad JSON) and
        # UnicodeDecodeError (raw bytes that aren't valid UTF-8). Use the
        # superclass so the helper degrades gracefully for either.
        return None
    code = body.get("code", "")
    match = _WARMUP_MARKER_RE.search(code)
    return match.group(1) if match else None


def _mock_warmup_probe_success(
    httpx_mock: HTTPXMock, sandbox_name: str = "sandbox-test"
) -> None:
    """Mock /exec so the warmup probe echoes its marker back on the first call.

    Used by tests that reach ``wait_until_ready`` success paths and don't
    otherwise care about the probe; they just need it to no-op quickly.
    """

    def _callback(request: httpx.Request) -> httpx.Response:
        marker = _extract_marker(request) or ""
        return httpx.Response(
            200,
            json={
                "stdout": f"{marker}\n",
                "stderr": "",
                "success": True,
                "execution_time_ms": 1,
            },
        )

    httpx_mock.add_callback(
        _callback,
        method="POST",
        url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/{sandbox_name}/exec",
        is_reusable=True,
    )


@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")
    monkeypatch.setenv("PROKUBE_USER_ID", "test-user@example.com")


@pytest.fixture
def config():
    """Create a test config."""
    return Config(
        api_url="https://test.example.com",
        workspace="test-ws",
        user_id="test-user@example.com",
    )


BASE = "https://test.example.com"
VERSION_RESPONSE = {"version": "0.8.0"}


def _mock_version(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/version", json=VERSION_RESPONSE
    )


def _mock_claim(httpx_mock: HTTPXMock, name="sandbox-test", phase="Running"):
    """Mock the claim endpoint with the v0.8 202 + full Sandbox body."""
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/claim",
        status_code=202,
        json={
            "name": name,
            "namespace": "test-ws",
            "phase": phase,
            "poolName": "python-pool",
            "claimName": f"{name}-claim",
            "lastError": None,
        },
    )


def _mock_pause(httpx_mock: HTTPXMock, name="sandbox-test", phase="Pausing"):
    """Mock the pause endpoint with the v0.8 202 + full Sandbox body."""
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/{name}/pause",
        status_code=202,
        json={"name": name, "namespace": "test-ws", "phase": phase},
    )


def _mock_get(httpx_mock: HTTPXMock, phase, name="sandbox-test", **extra):
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/{name}",
        json={"name": name, "phase": phase, **extra},
    )


class TestPausedStatus:
    """Tests for the lifecycle status enum."""

    def test_paused_status_exists(self):
        assert SandboxStatus.PAUSED.value == "Paused"

    def test_parse_paused_status(self):
        assert _parse_status("Paused", SandboxStatus.UNKNOWN) == SandboxStatus.PAUSED

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("Pausing", SandboxStatus.PAUSING),
            ("Resuming", SandboxStatus.RESUMING),
            ("Deleting", SandboxStatus.DELETING),
        ],
    )
    def test_parse_transitional_phases(self, wire, expected):
        assert _parse_status(wire, SandboxStatus.UNKNOWN) == expected
        assert expected.value == wire


class TestClientPause:
    """Tests for SandboxClient.pause()."""

    def test_pause_returns_pausing_sandbox(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_pause(httpx_mock, name="my-sandbox")

        client = SandboxClient(config)
        info = client.pause("my-sandbox")

        requests = httpx_mock.get_requests()
        pause_req = [r for r in requests if "/pause" in str(r.url)]
        assert len(pause_req) == 1
        assert pause_req[0].method == "POST"
        assert info.name == "my-sandbox"
        assert info.status == SandboxStatus.PAUSING
        client.close()

    def test_pause_conflict_raises_sandbox_error(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/my-sandbox/pause",
            status_code=409,
            json={
                "detail": "Cannot pause sandbox in phase 'Paused'. Only Running sandboxes can be paused."
            },
        )

        client = SandboxClient(config)
        with pytest.raises(SandboxError, match="Cannot pause sandbox"):
            client.pause("my-sandbox")
        client.close()


class TestClientResume:
    """Tests for SandboxClient.resume()."""

    def test_resume_success(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/my-sandbox/resume",
            status_code=202,
            json={"name": "my-sandbox", "namespace": "test-ws", "phase": "Resuming"},
        )

        client = SandboxClient(config)
        info = client.resume("my-sandbox")

        requests = httpx_mock.get_requests()
        resume_req = [r for r in requests if "/resume" in str(r.url)]
        assert len(resume_req) == 1
        assert resume_req[0].method == "POST"
        assert info.name == "my-sandbox"
        assert info.status == SandboxStatus.RESUMING
        client.close()

    def test_resume_conflict_raises_sandbox_error(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/my-sandbox/resume",
            status_code=409,
            json={
                "detail": "Cannot resume sandbox in phase 'Running'. Only Paused sandboxes can be resumed."
            },
        )

        client = SandboxClient(config)
        with pytest.raises(SandboxError, match="Cannot resume sandbox"):
            client.resume("my-sandbox")
        client.close()


class TestSandboxPause:
    """Tests for Sandbox.pause()."""

    def test_pause_waits_until_paused(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """pause() blocks by default and polls the async Pausing -> Paused hop."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        _mock_get(httpx_mock, "Pausing")
        _mock_get(httpx_mock, "Paused")

        sbx = Sandbox.from_pool("python-pool")
        assert sbx.status == "Running"

        sbx.pause()
        assert sbx.status == "Paused"

        polls = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "GET" and str(r.url).endswith("/sandboxes/sandbox-test")
        ]
        assert len(polls) == 2

        sbx._client.close()

    def test_pause_without_wait_reports_pausing(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)

        sbx = Sandbox.from_pool("python-pool")
        sbx.pause(wait=False)

        assert sbx.status == "Pausing"
        polls = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "GET" and str(r.url).endswith("/sandboxes/sandbox-test")
        ]
        assert not polls

        sbx._client.close()

    def test_pause_failure_raises_with_last_error(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        _mock_get(httpx_mock, "Pausing")
        _mock_get(httpx_mock, "Failed", lastError="pvc snapshot rejected by CSI driver")

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(
            SandboxError, match="pvc snapshot rejected by CSI driver"
        ) as excinfo:
            sbx.pause()
        assert "re-issue pause()" in str(excinfo.value)

        sbx._client.close()

    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    def test_pause_wait_timeout_raises(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        call_count = 0
        real_monotonic = __import__("time").monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return real_monotonic()
            return real_monotonic() + 1000

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        _mock_get(httpx_mock, "Pausing")

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError, match="did not pause within"):
            sbx.pause(timeout=1)

        sbx._client.close()

    def test_pause_wait_deleting_raises(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A delete admitted during the pause outranks it; stop waiting."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        _mock_get(httpx_mock, "Pausing")
        _mock_get(httpx_mock, "Deleting")

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxError, match="is being deleted"):
            sbx.pause()

        sbx._client.close()

    def test_pause_wait_sandbox_gone_raises(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A concurrent delete finishing mid-wait surfaces as SandboxError."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=404,
            json={"detail": "not found"},
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxError, match="deleted while waiting"):
            sbx.pause()

        sbx._client.close()

    def test_pause_non_running_raises(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/pause",
            status_code=409,
            json={
                "detail": "Cannot pause sandbox in phase 'Paused'. Only Running sandboxes can be paused."
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxError, match="Cannot pause sandbox"):
            sbx.pause()

        sbx._client.close()

    def test_pause_killed_sandbox_raises(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=202,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.kill()

        with pytest.raises(SandboxError, match="has been killed"):
            sbx.pause()


class TestSandboxResume:
    """Tests for Sandbox.resume()."""

    def test_resume_paused_sandbox(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/resume",
            status_code=202,
            json={"name": "sandbox-test", "namespace": "test-ws", "phase": "Resuming"},
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.pause(wait=False)
        assert sbx.status == "Pausing"

        sbx.resume()
        assert sbx.status == "Resuming"

        sbx._client.close()

    def test_resume_preserves_known_auto_idle_timeout(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_pause(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/resume",
            status_code=202,
            json={"name": "sandbox-test", "phase": "Resuming"},
        )

        sbx = Sandbox.from_pool("python-pool", auto_idle_timeout_seconds=900)
        sbx.pause(wait=False)
        sbx.resume()

        assert sbx.auto_idle_timeout_seconds == 900
        sbx._client.close()

    def test_resume_non_paused_raises(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/resume",
            status_code=409,
            json={
                "detail": "Cannot resume sandbox in phase 'Running'. Only Paused sandboxes can be resumed."
            },
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxError, match="Cannot resume sandbox"):
            sbx.resume()

        sbx._client.close()


class TestWaitUntilReady:
    """Tests for Sandbox.wait_until_ready()."""

    def test_wait_until_ready_immediate(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        # GET sandbox returns Running immediately
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )
        # wait_until_ready now runs a warmup probe after pod is Running
        _mock_warmup_probe_success(httpx_mock)

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=5)
        assert sbx.status == "Running"

        sbx._client.close()

    def test_wait_until_ready_after_pending(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        # First poll: Pending
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Pending"},
        )
        # Second poll: Running
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )
        # Warmup probe after Running
        _mock_warmup_probe_success(httpx_mock)

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=10)
        assert sbx.status == "Running"

        sbx._client.close()

    def test_wait_until_ready_polls_through_resuming(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Transitional phases are not terminal: keep polling until Running."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/resume",
            status_code=202,
            json={"name": "sandbox-test", "phase": "Resuming"},
        )
        _mock_get(httpx_mock, "Resuming")
        _mock_get(httpx_mock, "Running")
        _mock_warmup_probe_success(httpx_mock)

        sbx = Sandbox.from_pool("python-pool")
        sbx.resume()
        assert sbx.status == "Resuming"
        sbx.wait_until_ready(timeout=10)

        assert sbx.status == "Running"
        get_requests = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "GET" and str(r.url).endswith("/sandboxes/sandbox-test")
        ]
        assert len(get_requests) == 2
        sbx._client.close()

    def test_wait_until_ready_warms_kernel_on_cold_start(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """First run_code after pod Running returns empty; probe retries until marker."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        # First GET: Pending, second GET: Running
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Pending"},
        )
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        # First probe call: empty stdout (cold kernel).
        # Subsequent probe calls: echo the marker back (warm).
        call_counter = {"n": 0}

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            call_counter["n"] += 1
            body = json.loads(request.content)
            if call_counter["n"] > 1:
                assert "session_id" not in body
                assert body.get("reset_session") is True
            marker = _extract_marker(request) or ""
            if call_counter["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "stdout": "",
                        "stderr": "",
                        "success": True,
                        "execution_time_ms": 1,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "stdout": f"{marker}\n",
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/exec",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=10)

        assert sbx.status == "Running"
        # Probe should have been called at least twice (one empty, then success)
        assert call_counter["n"] >= 2

        sbx._client.close()

    def test_wait_until_ready_warm_kernel_no_extra_latency(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Warm kernel: the first probe succeeds, so there is exactly one probe call."""
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        call_counter = {"n": 0}

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            call_counter["n"] += 1
            marker = _extract_marker(request) or ""
            return httpx.Response(
                200,
                json={
                    "stdout": f"{marker}\n",
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/exec",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=10)

        assert sbx.status == "Running"
        assert call_counter["n"] == 1

        sbx._client.close()

    def test_wait_until_ready_warmup_timeout_does_not_raise(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """If the warmup probe never echoes the marker, wait_until_ready logs and returns."""
        # Make time.monotonic() advance quickly so the deadline trips after a
        # couple of probe attempts instead of burning real wall-clock time.
        import time as _time

        real_monotonic = _time.monotonic
        start = real_monotonic()
        tick = {"n": 0}

        def fake_monotonic() -> float:
            # Advance virtual time by 0.1s on every call, so a timeout=2
            # budget is exhausted after a handful of probes without being so
            # coarse that the extra monotonic() reads wait_until_ready takes
            # per-iteration (to bound each poll's request timeout) trip the
            # deadline before a single probe attempt happens.
            tick["n"] += 1
            return start + tick["n"] * 0.1

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)

        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        call_counter = {"n": 0}

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            call_counter["n"] += 1
            return httpx.Response(
                200,
                json={
                    "stdout": "",
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/exec",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        # Should return (not raise) even though the probe never echoes the marker.
        sbx.wait_until_ready(timeout=2)

        assert sbx.status == "Running"
        # Probe should have been attempted at least once.
        assert call_counter["n"] >= 1

        sbx._client.close()

    def test_wait_until_ready_retries_warmup_gateway_timeout(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Transient Agent Gateway 504s during warmup are retried."""
        monkeypatch.setattr("time.sleep", lambda _: None)

        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        call_counter = {"n": 0}

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return httpx.Response(504, text="upstream request timeout")
            marker = _extract_marker(request) or ""
            return httpx.Response(
                200,
                json={
                    "stdout": f"{marker}\n",
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/exec",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=5)

        assert sbx.status == "Running"
        assert call_counter["n"] == 2

        sbx._client.close()

    def test_wait_until_ready_warmup_accepts_extra_stdout(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A probe whose stdout contains the marker plus extra text succeeds in one attempt.

        Regression test for issue #51: the kernel may append unrelated
        warnings (e.g. IPython's history-thread SQLite error) to the same
        stdout as the marker. That session is live and must not be discarded.
        """
        monkeypatch.setattr("time.sleep", lambda _: None)

        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        call_counter = {"n": 0}

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            call_counter["n"] += 1
            marker = _extract_marker(request) or ""
            return httpx.Response(
                200,
                json={
                    "stdout": (
                        f"{marker}\n"
                        "The history saving thread hit an unexpected error "
                        "(OperationalError('attempt to write a readonly "
                        "database')). History will not be written to the "
                        "database.\n"
                    ),
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/exec",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=5)

        assert sbx.status == "Running"
        assert call_counter["n"] == 1

        sbx._client.close()

    def test_wait_until_ready_warmup_caps_per_probe_timeout(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Each warmup probe must be capped at 5s regardless of remaining budget.

        Without the per-probe cap, a single ``run_code`` call against a stuck
        kernel could block ``wait_until_ready`` for the user's entire timeout
        (e.g. 300s) and starve the intended retry loop. The probe should send
        ``timeout=5`` (the cap) to ``/exec`` even when the user passes a much
        larger ``wait_until_ready(timeout=...)``.
        """
        monkeypatch.setattr("time.sleep", lambda _: None)

        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        captured_timeouts: list[int] = []

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured_timeouts.append(body["timeout"])
            # Echo the marker so the probe succeeds on the first attempt.
            marker = _extract_marker(request) or ""
            return httpx.Response(
                200,
                json={
                    "stdout": f"{marker}\n",
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test/exec",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        # Pass a generously large overall timeout. The per-probe cap must
        # still keep individual /exec calls bounded to 5s.
        sbx.wait_until_ready(timeout=300)

        assert captured_timeouts, "warmup probe should have been called at least once"
        for sent_timeout in captured_timeouts:
            assert sent_timeout == 5, (
                f"probe sent timeout={sent_timeout}, expected cap of 5s "
                f"regardless of the wait_until_ready budget"
            )

        sbx._client.close()

    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    def test_wait_until_ready_timeout(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        # Make time.monotonic() advance past the deadline after first poll
        call_count = 0
        real_monotonic = __import__("time").monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return real_monotonic()
            # After first poll, jump past deadline
            return real_monotonic() + 1000

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)

        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        # Always return Pending
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Pending"},
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError, match="did not become ready"):
            sbx.wait_until_ready(timeout=1)

        sbx._client.close()

    def test_wait_until_ready_bounds_get_to_remaining_budget(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Each readiness poll's GET must be capped at the remaining budget.

        PROKUBE_TIMEOUT defaults to 300s and is used as the httpx client's
        per-request timeout. Without an explicit per-call override, a single
        stalled poll could block up to 300s even though the caller asked for
        a much shorter wait_until_ready(timeout=...) budget.
        """
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)

        captured_timeouts: list[dict[str, float] | None] = []

        def _get_callback(request: httpx.Request) -> httpx.Response:
            captured_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(
                200, json={"name": "sandbox-test", "phase": "Pending"}
            )

        httpx_mock.add_callback(
            _get_callback,
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError):
            sbx.wait_until_ready(timeout=3)

        assert captured_timeouts, "expected at least one GET poll"
        for sent_timeout in captured_timeouts:
            assert sent_timeout is not None, (
                "GET poll had no per-request timeout override, so it falls "
                "back to the client's 300s default instead of the caller's "
                "3s readiness budget"
            )
            assert sent_timeout["read"] <= 3, (
                f"GET poll allowed a {sent_timeout['read']}s read timeout, "
                f"but only 3s remained in the readiness budget"
            )

        sbx._client.close()

    def test_wait_until_ready_get_timeout_raises_sandbox_timeout_error(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A stalled poll must surface SandboxTimeoutError, not the raw httpx
        timeout exception, once the readiness budget is exhausted."""
        call_count = 0
        real_monotonic = __import__("time").monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return real_monotonic()
            return real_monotonic() + 1000

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)

        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_exception(
            httpx.ReadTimeout("simulated stalled backend"),
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            is_reusable=True,
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError, match="did not become ready"):
            sbx.wait_until_ready(timeout=1)

        sbx._client.close()

    def test_wait_until_ready_failed_includes_last_error(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_get(
            httpx_mock, "Failed", lastError="image pull backoff: manifest unknown"
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(
            SandboxError, match="image pull backoff: manifest unknown"
        ) as excinfo:
            sbx.wait_until_ready(timeout=10)
        assert "terminal state" in str(excinfo.value)

        sbx._client.close()

    def test_wait_until_ready_deleting_raises(self, mock_env, httpx_mock: HTTPXMock):
        """A sandbox being torn down can never become ready."""
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        _mock_get(httpx_mock, "Deleting")

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxError, match="'Deleting'"):
            sbx.wait_until_ready(timeout=10)

        sbx._client.close()


class TestSandboxKill:
    """Tests for the asynchronous Sandbox.kill()."""

    def test_kill_does_not_poll_by_default(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=202,
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.kill()

        assert not [
            r
            for r in httpx_mock.get_requests()
            if r.method == "GET" and "/api/" not in str(r.url)
        ]

    def test_kill_wait_polls_until_gone(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """kill(wait=True) polls the sandbox until the backend returns 404."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=202,
        )
        _mock_get(httpx_mock, "Deleting")
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=404,
            json={"detail": "not found"},
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.kill(wait=True)

        polls = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "GET" and str(r.url).endswith("/sandboxes/sandbox-test")
        ]
        assert len(polls) == 2
        with pytest.raises(SandboxError, match="has been killed"):
            sbx.run_code("print(1)")

    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    def test_kill_wait_timeout_raises(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        call_count = 0
        real_monotonic = __import__("time").monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return real_monotonic()
            return real_monotonic() + 1000

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=202,
        )
        _mock_get(httpx_mock, "Deleting")

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError, match="was not deleted within"):
            sbx.kill(wait=True, timeout=1)

        sbx._client.close()

    def test_kill_wait_delete_failed_raises_with_last_error(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A terminal delete_failed row surfaces immediately, with the reason."""
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=202,
        )
        _mock_get(httpx_mock, "Deleting")
        _mock_get(
            httpx_mock, "Failed", lastError="workspace purge could not be confirmed"
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(
            SandboxError,
            match=r"failed to delete: workspace purge could not be confirmed",
        ):
            sbx.kill(wait=True)

        # The delete was admitted and terminally failed: the object must not
        # accept work, but kill() stays re-issuable.
        with pytest.raises(SandboxError, match="is being deleted"):
            sbx.run_code("print(1)")
        sbx._client.close()

    def test_kill_wait_timeout_blocks_operations_and_kill_retries(
        self, mock_env, monkeypatch, httpx_mock: HTTPXMock
    ):
        """After a wait timeout the object is deletion-locked; kill() retries."""
        call_count = 0
        real_monotonic = __import__("time").monotonic

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return real_monotonic()
            return real_monotonic() + 1000

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=202,
        )
        _mock_get(httpx_mock, "Deleting")

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError, match="was not deleted within"):
            sbx.kill(wait=True, timeout=1)

        # Deletion was admitted: normal operations are blocked...
        with pytest.raises(SandboxError, match="is being deleted"):
            sbx.run_code("print(1)")

        # ...but kill() can be re-issued; a 404 on the re-issued DELETE means
        # the backend finished in the meantime, which is the desired outcome.
        httpx_mock.add_response(
            method="DELETE",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            status_code=404,
            json={"detail": "not found"},
        )
        sbx.kill(wait=True)
        assert sbx.status == "Succeeded"
        with pytest.raises(SandboxError, match="has been killed"):
            sbx.run_code("print(1)")


class TestSandboxPhaseProperty:
    """Tests for Sandbox.phase property."""

    def test_phase_returns_current_phase(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Paused"},
        )

        sbx = Sandbox.from_pool("python-pool")
        assert sbx.phase == "Paused"

        sbx._client.close()

    def test_phase_refreshes_from_api(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")
        # phase triggers a refresh (GET request)
        _ = sbx.phase

        requests = httpx_mock.get_requests()
        get_requests = [
            r
            for r in requests
            if r.method == "GET" and "/sandboxes/sandbox-test" in str(r.url)
        ]
        assert len(get_requests) == 1

        sbx._client.close()


class TestSandboxConnect:
    """Tests for Sandbox.connect() alias."""

    def test_connect_returns_same_result_as_get(self, mock_env, httpx_mock: HTTPXMock):
        """Sandbox.connect() should behave the same as Sandbox.get()."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/my-sandbox",
            json={"name": "my-sandbox", "phase": "Paused"},
        )

        sbx = Sandbox.connect("my-sandbox")
        assert sbx.name == "my-sandbox"
        assert sbx.status == "Paused"

        sbx._client.close()

    def test_connect_works(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes/my-sandbox",
            json={"name": "my-sandbox", "phase": "Running"},
        )

        sbx = Sandbox.connect("my-sandbox")
        assert sbx.name == "my-sandbox"
        assert sbx.status == "Running"

        sbx._client.close()


class TestListWithPhaseFilter:
    """Tests for Sandbox.list() with phase filter."""

    def test_list_filter_paused(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {"name": "sbx-1", "phase": "Running"},
                    {"name": "sbx-2", "phase": "Paused"},
                    {"name": "sbx-3", "phase": "Paused"},
                    {"name": "sbx-4", "phase": "Pending"},
                ],
            },
        )

        sandboxes = Sandbox.list(phase="Paused")

        assert len(sandboxes) == 2
        assert sandboxes[0].name == "sbx-2"
        assert sandboxes[0].status == "Paused"
        assert sandboxes[1].name == "sbx-3"
        assert sandboxes[1].status == "Paused"

        for sbx in sandboxes:
            sbx._client.close()

    def test_list_filter_no_matches(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {"name": "sbx-1", "phase": "Running"},
                ],
            },
        )

        sandboxes = Sandbox.list(phase="Paused")

        assert sandboxes == []

    def test_list_no_filter_returns_all(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/_platform/sandbox/test-ws/sandboxes",
            json={
                "sandboxes": [
                    {"name": "sbx-1", "phase": "Running"},
                    {"name": "sbx-2", "phase": "Paused"},
                ],
            },
        )

        sandboxes = Sandbox.list()

        assert len(sandboxes) == 2

        for sbx in sandboxes:
            sbx._client.close()
