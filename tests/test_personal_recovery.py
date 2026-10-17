from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from prokube.personal.cli import parser
from prokube.personal.client import AsyncSandbox
from prokube.personal.errors import SandboxError
from prokube.personal.ssh import _request


async def test_explicit_revert_does_not_implicitly_resume():
    client = SimpleNamespace(request=AsyncMock(return_value={"status": "SUSPENDED"}))
    sandbox = AsyncSandbox(client, SimpleNamespace(id="example"))
    await sandbox.revert()
    client.request.assert_awaited_once_with("POST", "v1/sandboxes/example/revert")


def test_cli_revert_is_available():
    args = parser().parse_args(["sandbox", "revert", "example"])
    assert args.action == "revert" and args.name == "example"


def test_connect_reports_recovery_without_reset(monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(status_code=412)

    monkeypatch.setattr("prokube.personal.ssh.httpx.request", request)
    with pytest.raises(SandboxError, match="pk sandbox revert"):
        _request(
            SimpleNamespace(api_url="https://example.invalid"),
            "fixture",
            "PUT",
            "/v1/sandboxes/example/ssh-key",
        )
    assert len(calls) == 1
