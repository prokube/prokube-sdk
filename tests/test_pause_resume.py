"""Tests for pause/resume functionality."""


import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError, SandboxTimeoutError
from prokube.sandbox import Sandbox
from prokube.sandbox.client import SandboxClient, _parse_status
from prokube.sandbox.models import SandboxStatus


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
VERSION_RESPONSE = {"version": "0.1.0"}


def _mock_version(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/version", json=VERSION_RESPONSE
    )


def _mock_claim(httpx_mock: HTTPXMock, name="sandbox-test", status="Running"):
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/namespaces/test-ws/sandboxes/claim",
        json={"name": name, "status": status},
    )


class TestPausedStatus:
    """Tests for Paused status enum."""

    def test_paused_status_exists(self):
        assert SandboxStatus.PAUSED.value == "Paused"

    def test_parse_paused_status(self):
        assert _parse_status("Paused", SandboxStatus.UNKNOWN) == SandboxStatus.PAUSED


class TestClientPause:
    """Tests for SandboxClient.pause()."""

    def test_pause_success(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/my-sandbox/pause",
            json={"status": "ok"},
        )

        client = SandboxClient(config)
        client.pause("my-sandbox")

        requests = httpx_mock.get_requests()
        pause_req = [r for r in requests if "/pause" in str(r.url)]
        assert len(pause_req) == 1
        assert pause_req[0].method == "POST"
        client.close()

    def test_pause_conflict_raises_sandbox_error(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/my-sandbox/pause",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/my-sandbox/resume",
            json={"status": "ok"},
        )

        client = SandboxClient(config)
        client.resume("my-sandbox")

        requests = httpx_mock.get_requests()
        resume_req = [r for r in requests if "/resume" in str(r.url)]
        assert len(resume_req) == 1
        assert resume_req[0].method == "POST"
        client.close()

    def test_resume_conflict_raises_sandbox_error(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/my-sandbox/resume",
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

    def test_pause_running_sandbox(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test/pause",
            json={"status": "ok"},
        )

        sbx = Sandbox.from_pool("python-pool")
        assert sbx.status == "Running"

        sbx.pause()
        assert sbx.status == "Paused"

        sbx._client.close()

    def test_pause_non_running_raises(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test/pause",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
            status_code=204,
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
        # Pause first
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test/pause",
            json={"status": "ok"},
        )
        # Resume
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test/resume",
            json={"status": "ok"},
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.pause()
        assert sbx.status == "Paused"

        sbx.resume()
        assert sbx.status == "Pending"

        sbx._client.close()

    def test_resume_non_paused_raises(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test/resume",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Pending"},
        )
        # Second poll: Running
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Running"},
        )

        sbx = Sandbox.from_pool("python-pool")
        sbx.wait_until_ready(timeout=10)
        assert sbx.status == "Running"

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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Pending"},
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxTimeoutError, match="did not become ready"):
            sbx.wait_until_ready(timeout=1)

        sbx._client.close()

    def test_wait_until_ready_terminal_state(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
            json={"name": "sandbox-test", "phase": "Failed"},
        )

        sbx = Sandbox.from_pool("python-pool")
        with pytest.raises(SandboxError, match="terminal state"):
            sbx.wait_until_ready(timeout=10)

        sbx._client.close()


class TestSandboxPhaseProperty:
    """Tests for Sandbox.phase property."""

    def test_phase_returns_current_phase(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        _mock_claim(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/sandbox-test",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/my-sandbox",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes/my-sandbox",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes",
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
            url=f"{BASE}/api/namespaces/test-ws/sandboxes",
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
