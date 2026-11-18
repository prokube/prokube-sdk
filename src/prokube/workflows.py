"""Connect Temporal's own Python client to a workspace's workflows.

The platform runs Temporal with one Temporal namespace per workspace. Its gRPC
API is at ``<domain>:443``; Temporal checks the Keycloak access token of the
workflows login (``pk auth login``) against the user's workspace roles.

``connect()`` only fills in address, namespace, TLS and that token and returns
an ordinary ``temporalio.client.Client``; everything else is Temporal's API::

    from prokube.workflows import connect

    client = await connect()  # platform and workspace from pk auth login
    handle = await client.start_workflow(
        "ResearchWithApproval", {"topic": "Otters"},
        id="otters-1", task_queue="research-with-approval",
    )

Needs the extra ``workflows`` (``pip install 'prokube-sdk[workflows]'``).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

from prokube import auth, endpoints

if TYPE_CHECKING:
    from temporalio.client import Client

# Strong references to the token refresh tasks, so they are not collected.
_refreshers: set[asyncio.Task[None]] = set()


def resolve(url: str | None = None, workspace: str | None = None) -> tuple[str, str]:
    """Platform URL and workspace: the arguments, else $PROKUBE_URL and
    $PROKUBE_WORKSPACE, else what pk auth login saved."""
    config = auth.load_config()
    url = url or os.environ.get("PROKUBE_URL") or config.get("url")
    workspace = (
        workspace or os.environ.get("PROKUBE_WORKSPACE") or config.get("workspace")
    )
    if not url:
        raise auth.LoginRequired("No platform. Run: pk auth login <url>")
    if not workspace:
        raise ValueError("No workspace. Pass workspace= or run: pk auth workspace set")
    return endpoints.normalize_url(url), workspace


def settings(url: str | None = None, workspace: str | None = None) -> dict[str, str]:
    """Address, namespace and API key for any Temporal client of the workspace.

    The keys are the Temporal CLI's environment variables; the API key is valid
    for a few minutes only.
    """
    url, workspace = resolve(url, workspace)
    session = auth.LoginSession(url, auth.WORKFLOWS)
    return {
        "TEMPORAL_ADDRESS": endpoints.grpc_target(url),
        "TEMPORAL_NAMESPACE": workspace,
        "TEMPORAL_TLS": "true",
        "TEMPORAL_API_KEY": session.token(),
    }


# Set on a worker the platform deploys into a workspace: Temporal's own
# variables plus the workspace's worker client in Keycloak.
WORKER_CLIENT_ENV = "PROKUBE_WORKFLOWS_CLIENT_ID"
WORKER_SECRET_ENV = "PROKUBE_WORKFLOWS_CLIENT_SECRET"
WORKER_TOKEN_URL_ENV = "PROKUBE_WORKFLOWS_TOKEN_URL"


class WorkerCredentials:
    """Access tokens of the workspace's worker client (client credentials)."""

    def __init__(self, token_url: str, client_id: str, client_secret: str) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> WorkerCredentials | None:
        """The worker client from the environment, if this is a deployed worker."""
        client_id = os.environ.get(WORKER_CLIENT_ENV)
        if not client_id:
            return None
        return cls(
            os.environ[WORKER_TOKEN_URL_ENV], client_id, os.environ[WORKER_SECRET_ENV]
        )

    def token(self) -> str:
        if time.time() < self._expires_at - auth.REFRESH_MARGIN_SECONDS:
            return self._token
        response = httpx.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 300))
        return self._token

    def expires_at(self) -> float:
        return self._expires_at


async def connect(
    url: str | None = None, workspace: str | None = None, **kwargs: Any
) -> Client:
    """A Temporal client for the workspace's namespace.

    On a worker the platform deployed, it uses the workspace's worker client
    and Temporal's address from the environment. Everywhere else it uses the
    login of ``pk auth login``. The token is renewed in the background while
    the event loop runs. Extra keyword arguments go to
    ``temporalio.client.Client.connect``.
    """
    from temporalio.client import Client  # pylint: disable=import-outside-toplevel

    worker = _worker() if url is None else None
    source: auth.LoginSession | WorkerCredentials
    if worker is not None:
        source = worker
        target = os.environ["TEMPORAL_ADDRESS"]
        workspace = workspace or os.environ["TEMPORAL_NAMESPACE"]
        tls = os.environ.get("TEMPORAL_TLS", "true").lower() != "false"
    else:
        url, workspace = resolve(url, workspace)
        source = auth.LoginSession(url, auth.WORKFLOWS)
        target, tls = endpoints.grpc_target(url), True
    token = await asyncio.to_thread(source.token)
    client = await Client.connect(
        target, namespace=workspace, tls=tls, api_key=token, **kwargs
    )
    task = asyncio.create_task(_keep_fresh(client, source))
    _refreshers.add(task)
    task.add_done_callback(_refreshers.discard)
    return client


def worker_token() -> str:
    """A current access token of the deployed worker's client.

    Activities can send it to other platform services, such as Agents over
    A2A at the gateway, as the workspace's workflows.
    """
    worker = _worker()
    if worker is None:
        raise RuntimeError(f"Not a deployed worker: ${WORKER_CLIENT_ENV} is unset.")
    return worker.token()


_shared: list[WorkerCredentials] = []


def _worker() -> WorkerCredentials | None:
    if not _shared:
        worker = WorkerCredentials.from_env()
        if worker is None:
            return None
        _shared.append(worker)
    return _shared[0]


async def _keep_fresh(
    client: Client, source: auth.LoginSession | WorkerCredentials
) -> None:
    while True:
        wait = source.expires_at() - time.time() - auth.REFRESH_MARGIN_SECONDS
        await asyncio.sleep(max(wait, 5))
        client.api_key = await asyncio.to_thread(source.token)
