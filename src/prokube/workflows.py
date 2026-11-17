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


async def connect(
    url: str | None = None, workspace: str | None = None, **kwargs: Any
) -> Client:
    """A Temporal client for the workspace's namespace, as the logged-in user.

    The token is renewed in the background while the event loop runs. Extra
    keyword arguments go to ``temporalio.client.Client.connect``.
    """
    from temporalio.client import Client  # pylint: disable=import-outside-toplevel

    url, workspace = resolve(url, workspace)
    session = auth.LoginSession(url, auth.WORKFLOWS)
    token = await asyncio.to_thread(session.token)
    client = await Client.connect(
        endpoints.grpc_target(url),
        namespace=workspace,
        tls=True,
        api_key=token,
        **kwargs,
    )
    task = asyncio.create_task(_keep_fresh(client, session))
    _refreshers.add(task)
    task.add_done_callback(_refreshers.discard)
    return client


async def _keep_fresh(client: Client, session: auth.LoginSession) -> None:
    while True:
        wait = session.expires_at() - time.time() - auth.REFRESH_MARGIN_SECONDS
        await asyncio.sleep(max(wait, 5))
        client.api_key = await asyncio.to_thread(session.token)
