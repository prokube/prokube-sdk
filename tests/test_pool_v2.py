"""Tests for the Sandbox v2 (Firecracker) warm pool (FirecrackerHibernatedPool).

All HTTP is mocked (pytest_httpx) — no live cluster required. Mirrors
test_pool.py and test_sandboxv2.py: covers create/list/get/delete pool and
claim, asserting the ``sandboxv2-pools`` route paths, request bodies, and claim
behavior, plus the api-key path prefix.
"""

import json

import pytest
from pytest_httpx import HTTPXMock

from prokube.common.config import Config
from prokube.common.exceptions import SandboxError
from prokube.sandboxv2 import SandboxV2, SandboxV2Client, SandboxV2Pool

BASE = "https://test.example.com"
NS = "test-ns"
POOLS = f"{BASE}/api/namespaces/{NS}/sandboxv2-pools"


@pytest.fixture
def config():
    return Config(api_url=BASE, workspace=NS, user_id="test-user@example.com")


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("PROKUBE_API_URL", BASE)
    monkeypatch.setenv("PROKUBE_WORKSPACE", NS)
    monkeypatch.setenv("PROKUBE_USER_ID", "test-user@example.com")
    monkeypatch.delenv("PROKUBE_API_KEY", raising=False)


def _mock_version(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/version", json={"version": "0.1.0"}
    )


def _pool_json(name="python-pool", size=3, ready=2):
    return {
        "name": name,
        "namespace": NS,
        "size": size,
        "readyMembers": ready,
        "members": [
            {"name": f"{name}-0", "phase": "Hibernated"},
            {"name": f"{name}-1", "phase": "Hibernated"},
        ],
        "image": "pk-sandbox-base",
        "runtimeClassName": "fc-host",
    }


def _sandbox_json(name="member-1", phase="Running", runtime="fc-host"):
    return {
        "name": name,
        "namespace": NS,
        "image": "pk-sandbox-base",
        "runtimeClassName": runtime,
        "phase": phase,
        "operatingMode": "Running",
        "terminalEnabled": True,
    }


# ---------------------------------------------------------------------------
# SandboxV2Client pool methods
# ---------------------------------------------------------------------------


class TestClientPool:
    def test_create_pool_path_and_body(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json()
        )
        from prokube.sandboxv2.models import CreateSandboxV2Request

        client = SandboxV2Client(config)
        template = CreateSandboxV2Request(
            name="python-pool",
            image="pk-sandbox-base",
            runtime_class_name="fc-host",
            vcpus=2,
            mem_mib=2048,
            egress=False,
        )
        info = client.create_pool(name="python-pool", size=3, template=template)

        req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        assert str(req.url) == POOLS
        body = json.loads(req.content)
        assert body["name"] == "python-pool"
        assert body["size"] == 3
        # template is nested, camelCase-aliased, None-pruned
        assert body["template"]["runtimeClassName"] == "fc-host"
        assert body["template"]["vcpus"] == 2
        assert body["template"]["memMiB"] == 2048
        assert info.name == "python-pool"
        assert info.size == 3
        assert info.ready_members == 2
        assert info.runtime_class_name == "fc-host"
        client.close()

    def test_list_pools(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=POOLS,
            json={"pools": [_pool_json("a", 2, 1), _pool_json("b", 1, 0)], "total": 2},
        )
        client = SandboxV2Client(config)
        pools = client.list_pools()
        assert [p.name for p in pools] == ["a", "b"]
        assert pools[0].ready_members == 1
        client.close()

    def test_get_pool(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=f"{POOLS}/python-pool", json=_pool_json()
        )
        client = SandboxV2Client(config)
        info = client.get_pool("python-pool")
        assert info.name == "python-pool"
        assert len(info.members) == 2
        assert info.members[0].phase == "Hibernated"
        client.close()

    def test_delete_pool(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="DELETE", url=f"{POOLS}/python-pool", status_code=204
        )
        client = SandboxV2Client(config)
        client.delete_pool("python-pool")
        req = [r for r in httpx_mock.get_requests() if r.method == "DELETE"][-1]
        assert str(req.url) == f"{POOLS}/python-pool"
        client.close()

    def test_claim_path_and_result(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{POOLS}/python-pool/claim",
            json=_sandbox_json(name="member-1", phase="Running"),
        )
        client = SandboxV2Client(config)
        info = client.claim("python-pool")
        req = [r for r in httpx_mock.get_requests() if r.method == "POST"][-1]
        assert str(req.url) == f"{POOLS}/python-pool/claim"
        assert info.name == "member-1"
        assert info.runtime_class == "fc-host"
        client.close()

    def test_claim_409_raises_sandbox_error(self, config, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{POOLS}/python-pool/claim",
            status_code=409,
            json={"detail": "no ready member"},
        )
        client = SandboxV2Client(config)
        with pytest.raises(SandboxError):
            client.claim("python-pool")
        client.close()


# ---------------------------------------------------------------------------
# SandboxV2Pool facade
# ---------------------------------------------------------------------------


class TestPoolFacade:
    def test_create_builds_fc_host_template(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json()
        )
        pool = SandboxV2Pool.create(
            name="python-pool", size=3, image="pk-sandbox-base", vcpus=2, mem_mib=2048
        )
        try:
            body = json.loads(
                [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
            )
            assert body["size"] == 3
            assert body["template"]["runtimeClassName"] == "fc-host"
            assert pool.name == "python-pool"
            assert pool.size == 3
            assert pool.ready_members == 2
            assert pool.runtime_class == "fc-host"
        finally:
            pool.close()

    def test_create_template_carries_env_and_secret_refs(
        self, mock_env, httpx_mock: HTTPXMock
    ):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="POST", url=POOLS, status_code=201, json=_pool_json()
        )
        pool = SandboxV2Pool.create(
            name="python-pool",
            size=3,
            image="pk-sandbox-base",
            env_vars={"FOO": "bar"},
            secret_refs=["openai-key"],
        )
        try:
            body = json.loads(
                [r for r in httpx_mock.get_requests() if r.method == "POST"][-1].content
            )
            # env/envFrom land in the nested pool-member template, CRD-shaped.
            assert body["template"]["env"] == [{"name": "FOO", "value": "bar"}]
            assert body["template"]["envFrom"] == [
                {"secretRef": {"name": "openai-key"}}
            ]
        finally:
            pool.close()

    def test_claim_returns_bound_sandbox(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=f"{POOLS}/python-pool", json=_pool_json()
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{POOLS}/python-pool/claim",
            json=_sandbox_json(name="member-1", phase="Running"),
        )
        pool = SandboxV2Pool.get("python-pool")
        sbx = pool.claim()
        try:
            assert isinstance(sbx, SandboxV2)
            assert sbx.name == "member-1"
            # pool's own client is independent of the claimed sandbox's client
            assert sbx._client is not pool._client
        finally:
            sbx._client.close()
            pool.close()

    def test_list_pools(self, mock_env, httpx_mock: HTTPXMock):
        _mock_version(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=POOLS,
            json={"pools": [_pool_json("a", 2, 1)], "total": 1},
        )
        pools = SandboxV2Pool.list()
        try:
            assert len(pools) == 1
            assert pools[0].name == "a"
        finally:
            for p in pools:
                p.close()


# ---------------------------------------------------------------------------
# api-key ORIGIN routing
# ---------------------------------------------------------------------------


def test_api_key_claim_uses_sandboxes_origin_route(httpx_mock: HTTPXMock):
    """Under api-key, claim hits the sandboxes ORIGIN /claim route (pool name in
    the body), NOT a pools sub-path — mirrors v1's ``claim_from_pool``."""
    cfg = Config(api_url="https://prokube.ai/pkui", workspace=NS, api_key="k")
    httpx_mock.add_response(
        method="POST",
        url=f"https://prokube.ai/sandboxv2/{NS}/sandboxes/claim",
        json=_sandbox_json(name="member-1", phase="Running"),
    )
    client = SandboxV2Client(cfg)
    info = client.claim("python-pool")
    req = httpx_mock.get_requests()[-1]
    assert req.headers["x-api-key"] == "k"
    assert str(req.url) == f"https://prokube.ai/sandboxv2/{NS}/sandboxes/claim"
    body = json.loads(req.content)
    assert body["poolName"] == "python-pool"
    assert info.name == "member-1"
    client.close()


def test_api_key_pool_crud_uses_origin_route(httpx_mock: HTTPXMock):
    """Pool CRUD under api-key targets a top-level ORIGIN path (no /pkui, no
    /api). Pool CRUD is not part of the deployed origin contract, but the path
    branch must still drop the UI prefix consistently with every other method."""
    cfg = Config(api_url="https://prokube.ai/pkui", workspace=NS, api_key="k")
    httpx_mock.add_response(
        method="GET",
        url=f"https://prokube.ai/sandboxv2/{NS}/pools",
        json={"pools": [], "total": 0},
    )
    client = SandboxV2Client(cfg)
    client.list_pools()
    req = httpx_mock.get_requests()[-1]
    assert req.headers["x-api-key"] == "k"
    assert str(req.url) == f"https://prokube.ai/sandboxv2/{NS}/pools"
    client.close()
