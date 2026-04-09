"""Tests for PoolClient and SandboxPool."""

import json
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError
from prokube.sandbox.pool import SandboxPool
from prokube.sandbox.pool_client import PoolClient

_WARMUP_MARKER_RE = re.compile(r'print\("(__pk_warmup_[0-9a-f]+__)"\)')


def _extract_marker(request: httpx.Request) -> str | None:
    """Extract the warmup marker from an /exec request body, if present."""
    try:
        body = json.loads(request.content)
    except ValueError:
        return None
    code = body.get("code", "")
    match = _WARMUP_MARKER_RE.search(code)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    """Create a test config (internal, no api_key)."""
    return Config(
        api_url="https://test.example.com",
        workspace="test-ws",
        user_id="test-user@example.com",
        api_key=None,
    )


@pytest.fixture
def config_api_key():
    """Create a test config with api_key (external)."""
    return Config(
        api_url="https://test.example.com",
        workspace="test-ws",
        api_key="sk-test-key",
    )


@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("PROKUBE_API_URL", "https://test.example.com")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "test-ws")
    monkeypatch.setenv("PROKUBE_USER_ID", "test-user@example.com")
    monkeypatch.delenv("PROKUBE_API_KEY", raising=False)


POOL_RESPONSE = {
    "name": "python-pool",
    "replicas": 3,
    "readyReplicas": 2,
    "image": "pk-sandbox-base:latest",
    "cpu": "2",
    "memory": "4Gi",
}


# ===========================================================================
# PoolClient tests
# ===========================================================================


class TestPoolClientPathRouting:
    """Test that PoolClient picks the right URL prefix."""

    def test_internal_path(self, config, httpx_mock: HTTPXMock):
        """Internal (no api_key) should use /api/namespaces/... prefix."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json={"pools": []},
        )

        client = PoolClient(config)
        client.list_pools()

        requests = httpx_mock.get_requests()
        list_req = [r for r in requests if "sandbox-pools" in str(r.url)]
        assert len(list_req) == 1
        assert "/api/namespaces/test-ws/sandbox-pools" in str(list_req[0].url)
        client.close()

    def test_api_key_path(self, config_api_key, httpx_mock: HTTPXMock):
        """External (api_key) should use /sandbox/<ws>/sandbox-pools prefix."""
        # No version mock needed: version check is skipped for api_key configs.
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/sandbox/test-ws/sandbox-pools",
            json={"pools": []},
        )

        client = PoolClient(config_api_key)
        client.list_pools()

        requests = httpx_mock.get_requests()
        list_req = [r for r in requests if "sandbox-pools" in str(r.url)]
        assert len(list_req) == 1
        assert "/sandbox/test-ws/sandbox-pools" in str(list_req[0].url)
        client.close()


class TestPoolClientCreate:
    """Tests for PoolClient.create_pool()."""

    def test_create_pool_sends_correct_body(self, config, httpx_mock: HTTPXMock):
        """create_pool should POST with poolSize in the body."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        client = PoolClient(config)
        info = client.create_pool(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
        )

        # Verify response mapping
        assert info.name == "python-pool"
        assert info.replicas == 3
        assert info.ready_replicas == 2
        assert info.image == "pk-sandbox-base:latest"
        assert info.cpu == "2"
        assert info.memory == "4Gi"

        # Verify request body
        requests = httpx_mock.get_requests()
        post_req = [r for r in requests if r.method == "POST"][0]
        body = json.loads(post_req.content)
        assert body["name"] == "python-pool"
        assert body["poolSize"] == 3
        assert body["cpu"] == "2"
        assert body["memory"] == "4Gi"

        client.close()


class TestPoolClientCreateExtras:
    """Tests for the extra create-time params added for backend parity."""

    def test_create_pool_omits_new_fields_when_unset(
        self, config, httpx_mock: HTTPXMock
    ):
        """Unset optional params should not appear in the request body."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        client = PoolClient(config)
        client.create_pool(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
        )

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
        body = json.loads(post_req.content)
        assert "allowInternetAccess" not in body
        assert "envVars" not in body
        assert "secretRefs" not in body
        client.close()

    def test_create_pool_sends_new_fields_with_camel_case(
        self, config, httpx_mock: HTTPXMock
    ):
        """allow_internet_access/env_vars/secret_refs should be sent as camelCase."""
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        client = PoolClient(config)
        client.create_pool(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
            allow_internet_access=True,
            env_vars=[
                {"name": "FOO", "value": "bar"},
                {"name": "HELLO", "value": "world"},
            ],
            secret_refs=["openai-key", "hf-token"],
        )

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
        body = json.loads(post_req.content)
        assert body["allowInternetAccess"] is True
        assert body["envVars"] == [
            {"name": "FOO", "value": "bar"},
            {"name": "HELLO", "value": "world"},
        ]
        assert body["secretRefs"] == ["openai-key", "hf-token"]
        # snake_case must NOT leak into the wire format
        assert "allow_internet_access" not in body
        assert "env_vars" not in body
        assert "secret_refs" not in body
        client.close()


class TestSandboxPoolCreateExtras:
    """High-level SandboxPool.create() tests for backend parity params."""

    def test_create_with_extras(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.create(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
            allow_internet_access=False,
            env_vars=[{"name": "DEBUG", "value": "1"}],
            secret_refs=["db-creds"],
            wait_until_ready=False,
        )

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
        body = json.loads(post_req.content)
        assert body["allowInternetAccess"] is False
        assert body["envVars"] == [{"name": "DEBUG", "value": "1"}]
        assert body["secretRefs"] == ["db-creds"]
        pool._client.close()

    def test_create_without_extras_omits_them(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.create(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
            wait_until_ready=False,
        )

        post_req = [r for r in httpx_mock.get_requests() if r.method == "POST"][0]
        body = json.loads(post_req.content)
        for key in ("allowInternetAccess", "envVars", "secretRefs"):
            assert key not in body
        pool._client.close()


class TestPoolClientList:
    """Tests for PoolClient.list_pools()."""

    def test_list_pools_empty(self, config, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json={"pools": []},
        )

        client = PoolClient(config)
        pools = client.list_pools()
        assert pools == []
        client.close()

    def test_list_pools_parses_response(self, config, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json={
                "pools": [
                    {
                        "name": "pool-a",
                        "replicas": 2,
                        "readyReplicas": 1,
                        "image": "img-a",
                        "cpu": "1",
                        "memory": "2Gi",
                    },
                    {
                        "name": "pool-b",
                        "replicas": 5,
                        "readyReplicas": 5,
                        "image": "img-b",
                        "cpu": "4",
                        "memory": "8Gi",
                    },
                ]
            },
        )

        client = PoolClient(config)
        pools = client.list_pools()
        assert len(pools) == 2
        assert pools[0].name == "pool-a"
        assert pools[0].replicas == 2
        assert pools[0].ready_replicas == 1
        assert pools[1].name == "pool-b"
        assert pools[1].replicas == 5
        assert pools[1].ready_replicas == 5
        client.close()


class TestPoolClientGet:
    """Tests for PoolClient.get_pool()."""

    def test_get_pool_returns_pool_info(self, config, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )

        client = PoolClient(config)
        info = client.get_pool("python-pool")
        assert info.name == "python-pool"
        assert info.replicas == 3
        assert info.ready_replicas == 2
        assert info.workspace == "test-ws"
        client.close()


class TestPoolClientDelete:
    """Tests for PoolClient.delete_pool()."""

    def test_delete_pool_sends_delete(self, config, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
        )
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            status_code=204,
        )

        client = PoolClient(config)
        client.delete_pool("python-pool")

        requests = httpx_mock.get_requests()
        delete_reqs = [r for r in requests if r.method == "DELETE"]
        assert len(delete_reqs) == 1
        assert "/sandbox-pools/python-pool" in str(delete_reqs[0].url)
        client.close()


# ===========================================================================
# SandboxPool high-level tests
# ===========================================================================


def _mock_version(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://test.example.com/api/version",
        json={"version": "0.1.0"},
    )


class TestSandboxPoolCreate:
    """Tests for SandboxPool.create()."""

    def test_create(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.create(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
            wait_until_ready=False,
        )

        assert pool.name == "python-pool"
        assert pool.workspace == "test-ws"
        assert pool.pool_size == 3
        assert pool.ready_replicas == 2
        assert pool.image == "pk-sandbox-base:latest"
        pool._client.close()


class TestSandboxPoolCreateWarmup:
    """Tests for the warm-one-pod behaviour of SandboxPool.create()."""

    # Pool response where the pool is already fully ready on the first refresh.
    _READY_POOL_RESPONSE = {
        "name": "python-pool",
        "replicas": 1,
        "readyReplicas": 1,
        "image": "pk-sandbox-base:latest",
        "cpu": "2",
        "memory": "4Gi",
    }

    def _mock_warmup_happy_path(
        self, httpx_mock: HTTPXMock, probe_marker_echo: bool = True
    ) -> dict:
        """Mock the full create -> refresh -> claim -> exec -> delete chain.

        Returns a dict with counters the test can inspect (number of /exec
        calls made).

        Args:
            probe_marker_echo: When True the /exec callback echoes the
                warmup marker back and the probe succeeds on the first try.
                When False it returns empty stdout so the probe retries
                until the deadline trips.
        """
        # Version check: hit twice (once for the PoolClient, once for the
        # SandboxClient spun up by Sandbox.from_pool).
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/version",
            json={"version": "0.1.0"},
            is_reusable=True,
        )
        # Pool create.
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=self._READY_POOL_RESPONSE,
        )
        # Pool refresh — reusable so the polling loop can read it repeatedly.
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=self._READY_POOL_RESPONSE,
            is_reusable=True,
        )
        # Claim a sandbox out of the pool.
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/claim",
            json={"name": "sandbox-warmup", "status": "Running"},
        )
        # wait_until_ready refreshes the sandbox before running the probe.
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-warmup",
            json={"name": "sandbox-warmup", "phase": "Running"},
            is_reusable=True,
        )

        counters: dict[str, int] = {"exec": 0}

        def _probe_callback(request: httpx.Request) -> httpx.Response:
            counters["exec"] += 1
            marker = _extract_marker(request) or ""
            stdout = f"{marker}\n" if probe_marker_echo else ""
            return httpx.Response(
                200,
                json={
                    "stdout": stdout,
                    "stderr": "",
                    "success": True,
                    "execution_time_ms": 1,
                },
            )

        httpx_mock.add_callback(
            _probe_callback,
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-warmup/exec",
            is_reusable=True,
        )
        # kill() at the end of the warmup probe.
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandboxes/sandbox-warmup",
            status_code=204,
        )
        return counters

    def test_sandbox_pool_create_warms_pods(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """Default create should claim+probe+kill one sandbox before returning."""
        counters = self._mock_warmup_happy_path(httpx_mock)

        pool = SandboxPool.create(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=1,
            cpu="2",
            memory="4Gi",
        )

        # The probe must have been invoked at least once — this is the whole
        # point of the warmup, and this is what makes from_pool safe.
        assert counters["exec"] >= 1, (
            "SandboxPool.create(wait_until_ready=True) should run the "
            "warmup probe before returning"
        )

        # The probe sandbox should have been killed.
        delete_reqs = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "DELETE" and "sandboxes/sandbox-warmup" in str(r.url)
        ]
        assert len(delete_reqs) == 1, (
            "warmup should kill the probe sandbox exactly once"
        )

        assert pool.name == "python-pool"
        pool._client.close()

    def test_sandbox_pool_create_opt_out(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        """wait_until_ready=False must preserve instant-return behaviour."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.create(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
            wait_until_ready=False,
        )

        # Absolutely no probe, refresh, claim, or kill calls should have
        # happened — only the version check and the pool POST.
        requests = httpx_mock.get_requests()
        exec_reqs = [r for r in requests if "/exec" in str(r.url)]
        claim_reqs = [r for r in requests if "/sandboxes/claim" in str(r.url)]
        refresh_reqs = [
            r
            for r in requests
            if r.method == "GET"
            and "/sandbox-pools/python-pool" in str(r.url)
        ]
        assert exec_reqs == [], "opt-out must not run warmup probe"
        assert claim_reqs == [], "opt-out must not claim a probe sandbox"
        assert refresh_reqs == [], "opt-out must not poll pool readiness"

        assert pool.name == "python-pool"
        pool._client.close()

    def test_sandbox_pool_create_warmup_timeout(
        self, mock_env, monkeypatch, caplog, httpx_mock: HTTPXMock
    ):
        """Probe timeout must be best-effort: log and return the pool anyway."""
        # Make time.monotonic() advance quickly so the probe deadline trips
        # after a couple of attempts instead of burning real wall-clock time.
        import logging
        import time as _time

        real_monotonic = _time.monotonic
        start = real_monotonic()
        tick = {"n": 0}

        def fake_monotonic() -> float:
            # Each call advances virtual time by 1s. The initial budget is
            # the one we pass in (ready_timeout=30 below), so the probe gets
            # a handful of attempts before the deadline is exhausted.
            tick["n"] += 1
            return start + tick["n"] * 1.0

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)

        # Probe never echoes the marker, so wait_until_ready will exhaust its
        # budget and log-and-return.
        counters = self._mock_warmup_happy_path(
            httpx_mock, probe_marker_echo=False
        )

        # Should NOT raise even though the probe never succeeds. Matches
        # Sandbox.wait_until_ready best-effort semantics.
        with caplog.at_level(logging.WARNING, logger="prokube.sandbox.sandbox"):
            pool = SandboxPool.create(
                name="python-pool",
                image="pk-sandbox-base:latest",
                pool_size=1,
                cpu="2",
                memory="4Gi",
                ready_timeout=30,
            )

        # The probe must have been attempted at least once before the
        # deadline tripped — this proves we actually ran the warmup probe
        # rather than bailing out early.
        assert counters["exec"] >= 1
        assert pool.name == "python-pool"
        # Sandbox._warmup_kernel must have logged the "did not echo marker
        # within deadline" warning. Without this assertion, the test would
        # silently pass even if the probe magically succeeded.
        assert any(
            "warmup probe did not echo marker" in record.message
            for record in caplog.records
        ), (
            "expected Sandbox._warmup_kernel to log a warning when the "
            "probe deadline is exhausted; saw: "
            f"{[record.message for record in caplog.records]}"
        )
        pool._client.close()


class TestSandboxPoolList:
    """Tests for SandboxPool.list()."""

    def test_list_empty(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json={"pools": []},
        )

        pools = SandboxPool.list()
        assert pools == []

    def test_list_multiple(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools",
            json={
                "pools": [
                    {
                        "name": "pool-a",
                        "replicas": 2,
                        "readyReplicas": 1,
                        "image": "img-a",
                        "cpu": "1",
                        "memory": "2Gi",
                    },
                    {
                        "name": "pool-b",
                        "replicas": 5,
                        "readyReplicas": 5,
                        "image": "img-b",
                        "cpu": "4",
                        "memory": "8Gi",
                    },
                ]
            },
        )

        pools = SandboxPool.list()
        assert len(pools) == 2
        assert pools[0].name == "pool-a"
        assert pools[0].workspace == "test-ws"
        assert pools[1].name == "pool-b"

        for p in pools:
            p._client.close()


class TestSandboxPoolGet:
    """Tests for SandboxPool.get()."""

    def test_get(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.get("python-pool")
        assert pool.name == "python-pool"
        assert pool.workspace == "test-ws"
        assert pool.pool_size == 3
        pool._client.close()


class TestSandboxPoolDelete:
    """Tests for SandboxPool.delete()."""

    def test_delete(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            status_code=204,
        )

        pool = SandboxPool.get("python-pool")
        pool.delete()

        requests = httpx_mock.get_requests()
        delete_reqs = [r for r in requests if r.method == "DELETE"]
        assert len(delete_reqs) == 1

    def test_delete_idempotent(self, mock_env, httpx_mock: HTTPXMock):
        """Calling delete() twice should not send a second DELETE request."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            status_code=204,
        )

        pool = SandboxPool.get("python-pool")
        pool.delete()
        pool.delete()  # Should be a no-op

        requests = httpx_mock.get_requests()
        delete_reqs = [r for r in requests if r.method == "DELETE"]
        assert len(delete_reqs) == 1

    def test_refresh_after_delete_raises(self, mock_env, httpx_mock: HTTPXMock):
        """refresh() on a deleted pool should raise SandboxError."""
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            status_code=204,
        )

        pool = SandboxPool.get("python-pool")
        pool.delete()

        with pytest.raises(SandboxError, match="has been deleted"):
            pool.refresh()


class TestSandboxPoolRefresh:
    """Tests for SandboxPool.refresh()."""

    def test_refresh_updates_fields(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )
        # Second GET for refresh with updated data
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json={
                "name": "python-pool",
                "replicas": 3,
                "readyReplicas": 3,
                "image": "pk-sandbox-base:latest",
                "cpu": "2",
                "memory": "4Gi",
            },
        )

        pool = SandboxPool.get("python-pool")
        assert pool.ready_replicas == 2

        pool.refresh()
        assert pool.ready_replicas == 3

        pool._client.close()


class TestSandboxPoolApiKeyRouting:
    """Tests for SandboxPool CRUD operations via the API key path."""

    def test_create_uses_api_key_path(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="POST",
            url="https://test.example.com/sandbox/ws/sandbox-pools",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.create(
            name="python-pool",
            image="pk-sandbox-base:latest",
            pool_size=3,
            cpu="2",
            memory="4Gi",
            api_url="https://test.example.com",
            workspace="ws",
            api_key="test-key",
            wait_until_ready=False,
        )

        requests = httpx_mock.get_requests()
        post_reqs = [r for r in requests if r.method == "POST"]
        assert len(post_reqs) == 1
        assert "/sandbox/ws/sandbox-pools" in str(post_reqs[0].url)
        pool._client.close()

    def test_list_uses_api_key_path(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/sandbox/ws/sandbox-pools",
            json={"pools": [POOL_RESPONSE]},
        )

        pools = SandboxPool.list(
            api_url="https://test.example.com",
            workspace="ws",
            api_key="test-key",
        )

        requests = httpx_mock.get_requests()
        get_reqs = [r for r in requests if r.method == "GET"]
        assert len(get_reqs) == 1
        assert "/sandbox/ws/sandbox-pools" in str(get_reqs[0].url)
        for p in pools:
            p._client.close()

    def test_get_uses_api_key_path(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/sandbox/ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.get(
            "python-pool",
            api_url="https://test.example.com",
            workspace="ws",
            api_key="test-key",
        )

        requests = httpx_mock.get_requests()
        get_reqs = [r for r in requests if r.method == "GET"]
        assert len(get_reqs) == 1
        assert "/sandbox/ws/sandbox-pools/python-pool" in str(get_reqs[0].url)
        pool._client.close()

    def test_delete_uses_api_key_path(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/sandbox/ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )
        httpx_mock.add_response(
            method="DELETE",
            url="https://test.example.com/sandbox/ws/sandbox-pools/python-pool",
            status_code=204,
        )

        pool = SandboxPool.get(
            "python-pool",
            api_url="https://test.example.com",
            workspace="ws",
            api_key="test-key",
        )
        pool.delete()

        requests = httpx_mock.get_requests()
        delete_reqs = [r for r in requests if r.method == "DELETE"]
        assert len(delete_reqs) == 1
        assert "/sandbox/ws/sandbox-pools/python-pool" in str(delete_reqs[0].url)


class TestSandboxPoolImport:
    """Test that SandboxPool is importable from prokube.sandbox."""

    def test_import_sandbox_pool(self):
        from prokube.sandbox import SandboxPool as ImportedPool

        assert ImportedPool is not None
        assert ImportedPool is SandboxPool


class TestSandboxPoolRepr:
    """Tests for SandboxPool.__repr__()."""

    def test_repr(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url="https://test.example.com/api/namespaces/test-ws/sandbox-pools/python-pool",
            json=POOL_RESPONSE,
        )

        pool = SandboxPool.get("python-pool")
        r = repr(pool)
        assert "python-pool" in r
        assert "3" in r
        pool._client.close()
