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
