"""Contract tests for the minimal pk-sandbox v0.1 client."""

import json
from typing import Any, cast

import httpx
import pytest
from pytest_httpx import HTTPXMock

from prokube import (
    SandboxAPIError,
    SandboxAuthorizationError,
    SandboxCapacityError,
    SandboxClient,
    SandboxTransportError,
)


def test_create_run_code_and_idempotent_delete(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://platform.example/pkui/api/namespaces/research/sandboxes",
        status_code=201,
        json={"name": "task-42", "phase": "Paused", "profile": "python"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://platform.example/pkui/api/namespaces/research/sandboxes/task-42/exec",
        json={
            "stdout": "42\n",
            "stderr": "",
            "success": True,
            "exitCode": 0,
            "durationMs": 3,
            "session_id": "session-1",
            "outputs": [],
            "traceback": None,
        },
    )
    httpx_mock.add_response(
        method="DELETE",
        url="https://platform.example/pkui/api/namespaces/research/sandboxes/task-42",
        status_code=204,
    )

    with SandboxClient(
        endpoint="https://platform.example/pkui",
        workspace="research",
        user_id="user@example.com",
    ) as client:
        sandbox = client.create(name="task-42")
        result = sandbox.run_code("print(42)", timeout=30)
        sandbox.delete()
        sandbox.delete()

    assert sandbox.metadata["phase"] == "Paused"
    assert result.stdout == "42\n"
    assert result.session_id == "session-1"
    requests = httpx_mock.get_requests()
    assert json.loads(requests[0].content) == {
        "name": "task-42",
        "runtime": "python",
        "size": "small",
        "network": "offline",
    }
    assert json.loads(requests[1].content) == {"code": "print(42)", "timeout": 30}
    assert requests[0].headers["kubeflow-userid"] == "user@example.com"
    assert [request.method for request in requests] == ["POST", "POST", "DELETE"]


def test_api_key_uses_public_gateway_route_and_origin(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://platform.example/sandbox/research/sandboxes",
        status_code=201,
        json={"name": "api-key-test"},
    )
    with SandboxClient(
        endpoint="https://platform.example/pkui",
        workspace="research",
        api_key="secret",
    ) as client:
        client.create(name="api-key-test")
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["x-api-key"] == "secret"


@pytest.mark.parametrize(
    ("status_code", "error_type", "retryable"),
    [
        (403, SandboxAuthorizationError, False),
        (503, SandboxCapacityError, True),
    ],
)
def test_typed_errors_retain_operation_context(
    httpx_mock: HTTPXMock,
    status_code: int,
    error_type: type[SandboxAPIError],
    retryable: bool,
) -> None:
    httpx_mock.add_response(
        status_code=status_code,
        headers={"x-request-id": "request-123", "retry-after": "2"},
        json={"detail": "not available", "cleanupRequired": True},
    )
    with SandboxClient(
        endpoint="https://platform.example",
        workspace="research",
        user_id="user@example.com",
    ) as client:
        with pytest.raises(error_type) as captured:
            client.create(name="task-42")
    error = captured.value
    assert error.status_code == status_code
    assert error.request_id == "request-123"
    assert error.operation == "create"
    assert error.sandbox_name == "task-42"
    assert error.retryable is retryable
    assert error.retry_after == "2"
    assert error.cleanup_required is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "Bad_Name"}, "DNS label"),
        ({"runtime": "node"}, "runtime must be python"),
        ({"size": "large"}, "size must be small"),
        ({"network": "internet"}, "network must be offline"),
    ],
)
def test_create_rejects_unsupported_contract_values(
    kwargs: dict[str, str], message: str
) -> None:
    with SandboxClient(
        endpoint="https://platform.example",
        workspace="research",
        user_id="user@example.com",
    ) as client:
        with pytest.raises(ValueError, match=message):
            client.create(**cast(Any, kwargs))


def test_credentials_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        SandboxClient(
            endpoint="https://platform.example",
            workspace="research",
            api_key="key",
            user_id="user@example.com",
        )


def test_delete_treats_backend_not_found_as_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=201, json={"name": "task-42"})
    httpx_mock.add_response(status_code=404, json={"detail": "missing"})
    with SandboxClient(
        endpoint="https://platform.example",
        workspace="research",
        user_id="user@example.com",
    ) as client:
        sandbox = client.create(name="task-42")
        sandbox.delete()


def test_transport_errors_are_typed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    with SandboxClient(
        endpoint="https://platform.example",
        workspace="research",
        user_id="user@example.com",
    ) as client:
        with pytest.raises(SandboxTransportError) as captured:
            client.create(name="task-42")
    assert captured.value.retryable is True
    assert captured.value.operation == "create"
