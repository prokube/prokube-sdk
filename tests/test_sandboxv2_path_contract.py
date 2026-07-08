"""Path-contract guard for the SandboxV2 backend cutover.

Phase 2 of the strangler-fig migration moves the SandboxV2 backend out of the
pkui monolith into a standalone ``fc-sandbox-api`` service. The cutover is
**path-based ingress reroute**: the *exact same* URL paths this SDK already
emits are re-pointed (at the ingress / Agent Gateway) to the new Service, on the
same host. That only works if these paths stay byte-identical, so this test pins
them. If a change here is intentional, it must be mirrored in ``fc-sandbox-api``
(``src/fc_sandbox_api/modules/external_proxy/sandboxv2_routes.py`` and
``modules/sandboxv2/routes.py``) *before* the contract is broken.

No HTTP is performed — we assert the client's path builders directly.
"""

from __future__ import annotations

from prokube.common.config import Config
from prokube.sandboxv2 import SandboxV2Client

NS = "test-ns"
BASE = "https://test.example.com"


def _api_key_client() -> SandboxV2Client:
    cfg = Config(api_url=BASE, workspace=NS, api_key="secret")
    return SandboxV2Client(cfg, check_version=False)


def _in_cluster_client() -> SandboxV2Client:
    cfg = Config(api_url=BASE, workspace=NS, user_id="u@example.com")
    return SandboxV2Client(cfg, check_version=False)


# ---------------------------------------------------------------------------
# External (api-key) origin routes -> ingress reroute of `/sandboxv2/*`
# ---------------------------------------------------------------------------


def test_api_key_paths_are_top_level_sandboxv2():
    c = _api_key_client()
    assert c._collection_path() == f"/sandboxv2/{NS}/sandboxes"
    assert c._sandbox_path("sbx") == f"/sandboxv2/{NS}/sandboxes/sbx"
    assert c._claim_path() == f"/sandboxv2/{NS}/sandboxes/claim"
    assert (
        c._sandbox_sub_path("sbx", "files/download")
        == f"/sandboxv2/{NS}/sandboxes/sbx/files/download"
    )
    # Every api-key path must live under the single top-level `/sandboxv2/`
    # prefix (no `/api`, no `/pkui`) so a single ingress rule reroutes them all.
    for p in (
        c._collection_path(),
        c._sandbox_path("sbx"),
        c._claim_path(),
        c._pools_path(),
        c._sandbox_sub_path("sbx", "exec"),
        c._sandbox_sub_path("sbx", "wait_ready"),
    ):
        assert p.startswith("/sandboxv2/"), p


# ---------------------------------------------------------------------------
# In-cluster (Agent Gateway / header-auth) routes -> reroute of
# `/api/namespaces/{ns}/sandboxv2*`
# ---------------------------------------------------------------------------


def test_in_cluster_paths_are_namespaced_under_api():
    c = _in_cluster_client()
    assert c._collection_path() == f"/api/namespaces/{NS}/sandboxv2"
    assert c._sandbox_path("sbx") == f"/api/namespaces/{NS}/sandboxv2/sbx"
    assert c._pools_path() == f"/api/namespaces/{NS}/sandboxv2-pools"
    assert c._pool_path("p") == f"/api/namespaces/{NS}/sandboxv2-pools/p"
    # In-cluster claim posts to the pool sub-path (pool in URL, no body).
    assert (
        f"{c._pool_path('p')}/claim"
        == f"/api/namespaces/{NS}/sandboxv2-pools/p/claim"
    )
    # The reroute matcher keys on the `sandboxv2` token after the namespace
    # segment; every in-cluster path must contain it under `/api/namespaces/`.
    for p in (
        c._collection_path(),
        c._sandbox_path("sbx"),
        c._pools_path(),
        c._sandbox_sub_path("sbx", "exec"),
    ):
        assert p.startswith(f"/api/namespaces/{NS}/sandboxv2"), p
