import time

import pytest

from prokube import auth, workflows
from tests.conftest import make_jwt

URL = "https://platform.example.com"


@pytest.fixture
def logged_in():
    auth.save_config(URL, "team-a")
    token = make_jwt(time.time() + 300)
    auth._save_tokens(
        URL,
        {"access_token": token, "refresh_token": "r", "expires_at": time.time() + 300},
        provider=auth.WORKFLOWS,
    )
    return token


def test_settings_point_at_the_workspace_namespace(logged_in):
    assert workflows.settings() == {
        "TEMPORAL_ADDRESS": "platform.example.com:443",
        "TEMPORAL_NAMESPACE": "team-a",
        "TEMPORAL_TLS": "true",
        "TEMPORAL_API_KEY": logged_in,
    }
    assert workflows.settings(workspace="team-b")["TEMPORAL_NAMESPACE"] == "team-b"


async def test_connect_passes_address_namespace_tls_and_token(monkeypatch, logged_in):
    from temporalio.client import Client

    calls = []

    class FakeClient:
        api_key = None

    async def fake_connect(target, **kwargs):
        calls.append((target, kwargs))
        return FakeClient()

    monkeypatch.setattr(Client, "connect", staticmethod(fake_connect))

    client = await workflows.connect(data_converter=None)

    assert isinstance(client, FakeClient)
    assert calls == [
        (
            "platform.example.com:443",
            {
                "namespace": "team-a",
                "tls": True,
                "api_key": logged_in,
                "data_converter": None,
            },
        )
    ]
    for task in list(workflows._refreshers):
        task.cancel()


def test_connect_needs_a_workspace(monkeypatch):
    auth.save_config(URL, None)
    monkeypatch.delenv("PROKUBE_WORKSPACE", raising=False)
    with pytest.raises(ValueError, match="No workspace"):
        workflows.resolve()


@pytest.fixture
def deployed_worker(monkeypatch, httpx_mock):
    monkeypatch.setattr(workflows, "_shared", [])
    for name, value in {
        "PROKUBE_WORKFLOWS_CLIENT_ID": "temporal-worker-team-a",
        "PROKUBE_WORKFLOWS_CLIENT_SECRET": "s3cret",
        "PROKUBE_WORKFLOWS_TOKEN_URL": "http://keycloak/token",
        "TEMPORAL_ADDRESS": "temporal-frontend.temporal.svc:7233",
        "TEMPORAL_NAMESPACE": "team-a",
        "TEMPORAL_TLS": "false",
    }.items():
        monkeypatch.setenv(name, value)
    httpx_mock.add_response(
        url="http://keycloak/token",
        method="POST",
        json={"access_token": "worker-token", "expires_in": 300},
    )
    return httpx_mock


async def test_connect_on_a_deployed_worker(monkeypatch, deployed_worker):
    from temporalio.client import Client

    calls = []

    class FakeClient:
        api_key = None

    async def fake_connect(target, **kwargs):
        calls.append((target, kwargs))
        return FakeClient()

    monkeypatch.setattr(Client, "connect", staticmethod(fake_connect))

    await workflows.connect()

    assert calls == [
        (
            "temporal-frontend.temporal.svc:7233",
            {"namespace": "team-a", "tls": False, "api_key": "worker-token"},
        )
    ]
    form = deployed_worker.get_request().content.decode()
    assert "grant_type=client_credentials" in form
    assert "client_id=temporal-worker-team-a" in form
    # Cached: activities get the same token without a second request.
    assert workflows.worker_token() == "worker-token"


def test_worker_token_needs_a_deployed_worker(monkeypatch):
    monkeypatch.setattr(workflows, "_shared", [])
    monkeypatch.delenv("PROKUBE_WORKFLOWS_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="Not a deployed worker"):
        workflows.worker_token()
