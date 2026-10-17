"""The pk commands. Each one is a thin wrapper over prokube.auth and prokube.sandbox."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import click
import httpx
from ate_env import EnvError
from websockets.exceptions import WebSocketException

from prokube import auth, endpoints
from prokube.cli import ssh
from prokube.sandbox import AsyncSandboxClient, actor_name
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import SANDBOX_PREFIX

T = TypeVar("T")


class _Group(click.Group):
    """Shows SDK errors as a one-line message instead of a traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (EnvError, auth.LoginError, httpx.HTTPError) as error:
            raise click.ClickException(str(error)) from error


def _target_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """Adds --url and --workspace; both default to what pk login saved."""
    command = click.option(
        "-w",
        "--workspace",
        envvar="PROKUBE_WORKSPACE",
        help="Workspace (default: $PROKUBE_WORKSPACE, then the one from pk login).",
    )(command)
    return click.option(
        "--url",
        envvar="PROKUBE_URL",
        help="Platform URL (default: $PROKUBE_URL, then the one from pk login).",
    )(command)


def _identity_option(command: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "-i",
        "--identity",
        type=click.Path(dir_okay=False, path_type=Path),
        help="Private SSH key whose .pub to install (default: ~/.ssh/id_ed25519, "
        "id_ecdsa or id_rsa).",
    )(command)


@click.group(cls=_Group)
@click.version_option(package_name="prokube-sdk", prog_name="pk")
def cli() -> None:
    """Command line for the prokube platform."""


# --- Login -----------------------------------------------------------------


@cli.command()
@click.argument("url")
@click.option("-w", "--workspace", help="Default workspace for sandbox commands.")
@click.option("--no-browser", is_flag=True, help="Only print the login URL.")
def login(url: str, workspace: str | None, no_browser: bool) -> None:
    """Log in to the platform at URL (https://<domain>).

    It becomes the current platform for the other commands.
    """
    url = _normalize(url)
    auth.login(url, open_browser=not no_browser)
    saved = _load_config()
    if workspace is None and saved.get("url") == url:
        workspace = saved.get("workspace")
    _save_config(url, workspace)
    click.echo(
        f"Logged in to {url}" + (f" (workspace {workspace})" if workspace else "")
    )


@cli.command()
@click.option("--url", envvar="PROKUBE_URL", help="Platform URL (default: current).")
def logout(url: str | None) -> None:
    """Forget the login for the current platform."""
    url = url or _load_config().get("url")
    if not url:
        raise click.ClickException("Not logged in.")
    auth.logout(url)
    click.echo(f"Logged out of {url}")


@cli.group("auth")
def auth_commands() -> None:
    """Show the login."""


@auth_commands.command("status")
@_target_options
def auth_status(url: str | None, workspace: str | None) -> None:
    """Show the current platform, workspace and user."""
    url, workspace = _resolve(url, workspace)
    if not url:
        raise click.ClickException("Not logged in. Run: pk login <url>")
    click.echo(f"Platform:  {url}")
    click.echo(f"Workspace: {workspace or '-'}")
    claims = _claims(_token(url))
    expires = datetime.fromtimestamp(claims["exp"], timezone.utc)
    click.echo(f"User:      {claims.get('email') or claims.get('sub')}")
    click.echo(f"Token:     valid until {expires:%Y-%m-%d %H:%M} UTC")


# --- Sandboxes -------------------------------------------------------------


@cli.group()
def sandbox() -> None:
    """Create and use sandboxes in a workspace."""


@sandbox.command()
@click.argument("name")
@click.option("-t", "--template", required=True, help="ActorTemplate to start from.")
@click.option(
    "--template-atespace",
    default=endpoints.TEMPLATE_ATESPACE,
    show_default=True,
    help="Atespace that holds the template.",
)
@click.option("--connect", is_flag=True, help="Open an SSH session afterwards.")
@_identity_option
@_target_options
def create(
    name: str,
    template: str,
    template_atespace: str,
    connect: bool,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
) -> None:
    """Create sandbox NAME from a template."""
    actor = _with_client(
        url,
        workspace,
        lambda client: client.create(
            name, template=template, template_atespace=template_atespace
        ),
    )
    click.echo(f"Created {_name(actor)} ({_state(actor)})")
    if connect:
        _connect(name, identity, url, workspace)


@sandbox.command("list")
@_target_options
def list_(url: str | None, workspace: str | None) -> None:
    """List the sandboxes of the workspace."""
    actors = _with_client(url, workspace, lambda client: client.list())
    rows = [("NAME", "STATE", "TEMPLATE")]
    rows += [(_name(a), _state(a), a.actor_template.name) for a in actors]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        click.echo(
            "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip()
        )


@sandbox.command()
@click.argument("name")
@_identity_option
@_target_options
def connect(
    name: str, identity: Path | None, url: str | None, workspace: str | None
) -> None:
    """Open an SSH session to sandbox NAME.

    Installs your public key in the sandbox, pins its host key and runs ssh.
    """
    _connect(name, identity, url, workspace)


@sandbox.command("ssh-config")
@click.argument("name")
@_identity_option
@_target_options
def ssh_config(
    name: str, identity: Path | None, url: str | None, workspace: str | None
) -> None:
    """Print an OpenSSH config entry for sandbox NAME.

    Installs your public key and pins the host key like connect. To use the
    entries from ssh and IDEs, add this line at the top of ~/.ssh/config:
    Include ~/.config/prokube/ssh/hosts/*.conf
    """
    _, config_file = _prepare_ssh(name, identity, url, workspace)
    click.echo(config_file.read_text(), nl=False)


@sandbox.command()
@click.argument("name")
@_target_options
def proxy(name: str, url: str | None, workspace: str | None) -> None:
    """Carry SSH to sandbox NAME over a WebSocket (OpenSSH ProxyCommand)."""
    url, workspace = _require(url, workspace)
    websocket_url = endpoints.ssh_url(url, workspace, actor_name(name))
    try:
        ssh.proxy(websocket_url, _token(url))
    except (WebSocketException, OSError) as error:
        raise click.ClickException(
            f"SSH connection to {name} failed: {error}"
        ) from error


@sandbox.command("exec")
@click.argument("name")
@click.argument("command", nargs=-1, required=True)
@_target_options
def exec_(
    name: str, command: tuple[str, ...], url: str | None, workspace: str | None
) -> None:
    """Run COMMAND in sandbox NAME and exit with its exit code.

    Put -- before the command: pk sandbox exec demo -- ls -l /workspace
    """
    sys.exit(
        _with_client(url, workspace, lambda client: _stream(client, name, command))
    )


@sandbox.command()
@click.argument("source")
@click.argument("destination")
@_target_options
def cp(source: str, destination: str, url: str | None, workspace: str | None) -> None:
    """Copy a file to or from a sandbox.

    \b
    pk sandbox cp train.py demo:/workspace/
    pk sandbox cp demo:/workspace/model.pt .
    """
    remote_source, remote_destination = _remote(source), _remote(destination)
    if (remote_source is None) == (remote_destination is None):
        raise click.UsageError(
            "Exactly one of SOURCE and DESTINATION must be NAME:PATH."
        )
    if remote_destination:
        name, path = remote_destination
        local = Path(source)
        if path.endswith("/"):
            path += local.name
        data, mode = local.read_bytes(), local.stat().st_mode & 0o777
        _with_client(
            url,
            workspace,
            lambda client: client.write_file(name, path, data, mode=mode),
        )
    elif remote_source:
        name, path = remote_source
        data = _with_client(url, workspace, lambda client: client.read_file(name, path))
        local = Path(destination)
        if local.is_dir():
            local /= PurePosixPath(path).name
        local.write_bytes(data)


@sandbox.command()
@click.argument("name")
@_target_options
def suspend(name: str, url: str | None, workspace: str | None) -> None:
    """Suspend sandbox NAME; its next use resumes it."""
    actor = _with_client(url, workspace, lambda client: client.suspend(name))
    click.echo(f"{_name(actor)}: {_state(actor)}")


@sandbox.command()
@click.argument("name")
@_target_options
def resume(name: str, url: str | None, workspace: str | None) -> None:
    """Resume sandbox NAME."""
    actor = _with_client(url, workspace, lambda client: client.resume(name))
    click.echo(f"{_name(actor)}: {_state(actor)}")


@sandbox.command()
@click.argument("name")
@_target_options
def delete(name: str, url: str | None, workspace: str | None) -> None:
    """Delete sandbox NAME and its workspace volume."""
    url, workspace = _require(url, workspace)
    actor = _with_client(url, workspace, lambda client: client.delete(name))
    ssh.forget_host(ssh.host_alias(workspace, _name(actor)))
    click.echo(f"Deleted {_name(actor)}")


# --- Helpers ---------------------------------------------------------------


def _connect(
    name: str, identity: Path | None, url: str | None, workspace: str | None
) -> None:
    alias, config_file = _prepare_ssh(name, identity, url, workspace)
    os.execvp("ssh", ["ssh", "-F", str(config_file), alias])


def _prepare_ssh(
    name: str, identity: Path | None, url: str | None, workspace: str | None
) -> tuple[str, Path]:
    """Install the user's key, pin the host key and write the ssh config entry."""
    url, workspace = _require(url, workspace)
    identity = identity or ssh.default_identity()
    if identity is None:
        raise click.ClickException(
            "No SSH key in ~/.ssh. Create one with ssh-keygen -t ed25519 "
            "or pass --identity."
        )
    public_key = identity.with_name(identity.name + ".pub").read_text()

    async def install_key(client: AsyncSandboxClient) -> str:
        host_key = (await client.read_file(name, ssh.HOST_KEY_PATH)).decode()
        current = (await client.read_file(name, ssh.AUTHORIZED_KEYS_PATH)).decode()
        updated = ssh.add_authorized_key(current, public_key)
        if updated != current:
            await client.write_file(name, ssh.AUTHORIZED_KEYS_PATH, updated)
        return host_key

    host_key = _with_client(url, workspace, install_key)
    alias = ssh.host_alias(workspace, actor_name(name).removeprefix(SANDBOX_PREFIX))
    ssh.pin_host_key(alias, host_key)
    proxy_command = [sys.executable, "-m", "prokube.cli", "sandbox", "proxy", name]
    proxy_command += ["--url", url, "--workspace", workspace]
    return alias, ssh.write_host_config(alias, proxy_command, identity)


async def _stream(
    client: AsyncSandboxClient, name: str, command: tuple[str, ...]
) -> int:
    """Run command, copy its output as it arrives and return its exit code."""
    process = await client.env(name).start_process(command)
    async for output in process.output(follow=True):
        if output.stdout is not None:
            click.echo(output.stdout, nl=False)
        elif output.stderr is not None:
            click.echo(output.stderr, nl=False, err=True)
        elif output.exit is not None:
            return output.exit.exit_code
    raise click.ClickException("The output stream ended before the command exited.")


def _with_client(
    url: str | None,
    workspace: str | None,
    action: Callable[[AsyncSandboxClient], Awaitable[T]],
) -> T:
    """Run action with a sandbox client for the workspace."""
    url, workspace = _require(url, workspace)

    async def run() -> T:
        async with AsyncSandboxClient(url, workspace) as client:
            return await action(client)

    try:
        return asyncio.run(run())
    except auth.LoginRequired as error:
        raise click.ClickException(
            f"Not logged in to {url}. Run: pk login {url}"
        ) from error


def _token(url: str) -> str:
    try:
        return auth.LoginSession(url).token()
    except auth.LoginRequired as error:
        raise click.ClickException(
            f"Not logged in to {url}. Run: pk login {url}"
        ) from error


def _resolve(url: str | None, workspace: str | None) -> tuple[str | None, str | None]:
    """Fill in url and workspace from the saved config."""
    saved = _load_config()
    url = url or saved.get("url")
    return (_normalize(url) if url else None), workspace or saved.get("workspace")


def _require(url: str | None, workspace: str | None) -> tuple[str, str]:
    url, workspace = _resolve(url, workspace)
    if not url:
        raise click.UsageError("No platform URL. Run: pk login <url>")
    if not workspace:
        raise click.UsageError(
            "No workspace. Pass --workspace or run: pk login <url> --workspace <name>"
        )
    return url, workspace


def _normalize(url: str) -> str:
    try:
        return endpoints.normalize_url(url)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="URL") from error


def _remote(argument: str) -> tuple[str, str] | None:
    """Split NAME:PATH; None for a local path."""
    name, colon, path = argument.partition(":")
    if not colon or not name or "/" in name:
        return None
    return name, path


def _name(actor: ateapi.Actor) -> str:
    return actor.metadata.name.removeprefix(SANDBOX_PREFIX)


def _state(actor: ateapi.Actor) -> str:
    return ateapi.ActorState.Name(actor.status.state).removeprefix("ACTOR_STATE_")


def _claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it; only used for display."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


# --- Saved platform and workspace ------------------------------------------


def _config_file() -> Path:
    return auth.config_dir() / "config.json"


def _load_config() -> dict[str, str]:
    path = _config_file()
    return json.loads(path.read_text()) if path.exists() else {}


def _save_config(url: str, workspace: str | None) -> None:
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = {"url": url} | ({"workspace": workspace} if workspace else {})
    path.write_text(json.dumps(config, indent=2) + "\n")
