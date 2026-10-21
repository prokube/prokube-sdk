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
from prokube.cli import banner, ssh
from prokube.sandbox import AsyncSandboxClient, PresetError, Presets, actor_name
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import SANDBOX_PREFIX

T = TypeVar("T")


class _Group(click.Group):
    """Shows SDK errors as a one-line message instead of a traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (EnvError, auth.LoginError, httpx.HTTPError, PresetError) as error:
            raise click.ClickException(str(error)) from error

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if ctx.parent is None and banner.wanted():
            formatter.write(banner.render() + "\n\n")
        super().format_help(ctx, formatter)


def _show_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    if banner.wanted():
        click.echo(banner.render())
    else:
        click.echo(f"pk, version {banner.pk_version()}")
    ctx.exit()


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


def _install_ssh_option(command: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--install-ssh",
        is_flag=True,
        help="Also make the sandbox reachable as an ssh host from ~/.ssh/config "
        "(ssh, VS Code Remote-SSH, JetBrains Gateway).",
    )(command)


@click.group(cls=_Group)
@click.option(
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_show_version,
    help="Show the version and exit.",
)
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
    saved = auth.load_config()
    if workspace is None and saved.get("url") == url:
        workspace = saved.get("workspace")
    auth.save_config(url, workspace)
    click.echo(
        f"Logged in to {url}" + (f" (workspace {workspace})" if workspace else "")
    )


@cli.command()
@click.option("--url", envvar="PROKUBE_URL", help="Platform URL (default: current).")
def logout(url: str | None) -> None:
    """Forget the login for the current platform."""
    url = url or auth.load_config().get("url")
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
    if os.environ.get(auth.API_KEY_ENV):
        click.echo(f"User:      API key (${auth.API_KEY_ENV})")
        return
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
@click.option(
    "-p",
    "--preset",
    help="Preset to start from (default: the platform's default preset).",
)
@click.option("--connect", is_flag=True, help="Open an SSH session afterwards.")
@_install_ssh_option
@_identity_option
@_target_options
def create(
    name: str,
    preset: str | None,
    connect: bool,
    install_ssh: bool,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
) -> None:
    """Create sandbox NAME from a preset, with the platform's network rules."""

    async def run(client: AsyncSandboxClient) -> tuple[ateapi.Actor, Presets]:
        return await client.create(name, preset=preset), await client.presets()

    actor, presets = _with_client(url, workspace, run)
    click.echo(f"Created {_name(actor)} ({_summary(actor, presets)})")
    if connect:
        _connect(name, identity, url, workspace, install_ssh)
    elif install_ssh:
        alias, _ = _prepare_ssh(name, identity, url, workspace)
        _install_ssh(alias)


@sandbox.command("list")
@_target_options
def list_(url: str | None, workspace: str | None) -> None:
    """List the sandboxes of the workspace with their preset."""

    async def collect(
        client: AsyncSandboxClient,
    ) -> tuple[list[ateapi.Actor], Presets | None]:
        actors = await client.list()
        try:
            return actors, await client.presets()
        except (PresetError, httpx.HTTPError) as error:
            click.echo(f"Warning: {error}; showing templates instead.", err=True)
            return actors, None

    actors, presets = _with_client(url, workspace, collect)
    rows = [("NAME", "STATE", "PRESET", "SIZE")]
    rows += [(_name(a), _state(a), *_preset_of(a, presets)) for a in actors]
    _table(rows)


@sandbox.command()
@click.argument("name")
@_install_ssh_option
@_identity_option
@_target_options
def connect(
    name: str,
    install_ssh: bool,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
) -> None:
    """Open an SSH session to sandbox NAME.

    Installs your public key in the sandbox, pins its host key and runs ssh.
    """
    _connect(name, identity, url, workspace, install_ssh)


@sandbox.command("ssh-config")
@click.argument("name", required=False)
@click.option(
    "--install",
    is_flag=True,
    help="Include all pk ssh hosts from ~/.ssh/config instead of printing the "
    "entry. NAME is optional then.",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Let ssh and IDEs resume the sandbox when it is suspended.",
)
@_identity_option
@_target_options
def ssh_config(
    name: str | None,
    install: bool,
    resume: bool,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
) -> None:
    """Print an OpenSSH config entry for sandbox NAME.

    Installs your public key and pins the host key like connect. With --install,
    adds an Include of the entries of all sandboxes at the top of
    ~/.ssh/config once, so ssh and IDEs that read it find them.

    ssh through the entry refuses a suspended sandbox, so an IDE that reconnects
    on its own does not wake it; --resume lets it resume the sandbox instead.
    connect, forward and create keep the entry's setting.
    """
    if not install:
        if name is None:
            raise click.UsageError("Pass NAME, or --install.")
        _, config_file = _prepare_ssh(name, identity, url, workspace, resume)
        click.echo(config_file.read_text(), nl=False)
        return
    alias = _prepare_ssh(name, identity, url, workspace, resume)[0] if name else None
    _install_ssh(alias)


@sandbox.command()
@click.argument("name")
@click.option("--resume", is_flag=True, help="Resume the sandbox if it is suspended.")
@_target_options
def proxy(name: str, resume: bool, url: str | None, workspace: str | None) -> None:
    """Carry SSH to sandbox NAME over a WebSocket (OpenSSH ProxyCommand).

    Refuses a suspended sandbox unless --resume is given: connecting would
    resume it.
    """
    url, workspace = _require(url, workspace)
    if not resume:
        actor = _with_client(url, workspace, lambda client: client.get(name))
        if actor.status.state in (
            ateapi.ACTOR_STATE_SUSPENDED,
            ateapi.ACTOR_STATE_SUSPENDING,
        ):
            raise click.ClickException(
                f"{name} is suspended, and ssh does not resume it. "
                f"Run: pk sandbox resume {name}"
            )
    websocket_url = endpoints.ssh_url(url, workspace, actor_name(name))
    try:
        ssh.proxy(websocket_url, _credential(url))
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
        _with_env_guest(
            url, workspace, name, lambda client: _stream(client, name, command)
        )
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
        _with_env_guest(
            url,
            workspace,
            name,
            lambda client: client.write_file(name, path, data, mode=mode),
        )
    elif remote_source:
        name, path = remote_source
        data = _with_env_guest(
            url, workspace, name, lambda client: client.read_file(name, path)
        )
        local = Path(destination)
        if local.is_dir():
            local /= PurePosixPath(path).name
        local.write_bytes(data)


@sandbox.command()
@click.argument("name")
@click.argument("remote_port", type=click.IntRange(1, 65535))
@click.option(
    "--local-port",
    type=click.IntRange(1, 65535),
    help="Port on localhost (default: REMOTE_PORT).",
)
@_identity_option
@_target_options
def forward(
    name: str,
    remote_port: int,
    local_port: int | None,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
) -> None:
    """Forward localhost to REMOTE_PORT in sandbox NAME until Ctrl-C.

    The tunnel runs over SSH, like connect, and listens on localhost only.
    For example, after `opencode serve --port 4096` in the sandbox:

    \b
    pk sandbox forward demo 4096
    opencode --server http://localhost:4096
    """
    local_port = local_port or remote_port
    if ssh.port_in_use(local_port):
        raise click.ClickException(
            f"Local port {local_port} is in use. Pass another one with --local-port."
        )
    listening = _with_env_guest(
        url, workspace, name, lambda client: _listens(client, name, remote_port)
    )
    if not listening:
        raise click.ClickException(
            f"Nothing listens on port {remote_port} in sandbox {name}. Start the "
            "server there first."
        )
    alias, config_file = _prepare_ssh(name, identity, url, workspace)
    click.echo(
        f"Forwarding http://localhost:{local_port} to port {remote_port} in {name}. "
        "Press Ctrl-C to stop."
    )
    sys.stdout.flush()  # exec does not flush Python's buffers
    os.execvp("ssh", ssh.forward_args(config_file, alias, local_port, remote_port))


@sandbox.command()
@click.argument("name")
@_target_options
def suspend(name: str, url: str | None, workspace: str | None) -> None:
    """Suspend sandbox NAME; its next use resumes it, except plain ssh."""
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
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@_target_options
def revert(name: str, yes: bool, url: str | None, workspace: str | None) -> None:
    """Return sandbox NAME to its last snapshot, suspended.

    Use it for a crashed sandbox, for example after a node restart.
    Everything since the last snapshot is lost. Asks first unless the sandbox
    crashed: its changes are gone already.
    """
    if not yes:
        current = _with_client(url, workspace, lambda client: client.get(name))
        if current.status.state == ateapi.ACTOR_STATE_CRASHED:
            click.echo(f"{name} crashed; its changes since the last snapshot are lost.")
        else:
            click.confirm(
                f"Discard all changes to {name} since its last snapshot?", abort=True
            )
    actor = _with_client(url, workspace, lambda client: client.revert(name))
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


@sandbox.command()
@click.argument("name")
@click.option("-p", "--preset", required=True, help="Preset with the sandbox's image.")
@_target_options
def resize(name: str, preset: str, url: str | None, workspace: str | None) -> None:
    """Switch sandbox NAME to another preset, e.g. a larger one.

    The preset must have the sandbox's image and snapshot fidelity. A running
    sandbox is suspended and resumed: /workspace is kept, running processes
    restart. A suspended sandbox stays suspended. Network rules and
    credentials stay.
    """
    click.echo(f"Resizing {name}: /workspace is kept, running processes restart.")

    async def run(client: AsyncSandboxClient) -> tuple[ateapi.Actor, Presets]:
        return await client.resize(name, preset=preset), await client.presets()

    actor, presets = _with_client(url, workspace, run)
    click.echo(f"{_name(actor)}: {_summary(actor, presets)}")


@sandbox.command()
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    help="Serve streamable HTTP on this port instead of stdio.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="With --port.")
@_target_options
def mcp(port: int | None, host: str, url: str | None, workspace: str | None) -> None:
    """Serve the sandbox tools to MCP clients, over stdio by default.

    \b
    claude mcp add sandbox -- pk sandbox mcp

    In a workspace pod with sandbox access it needs no login and acts as the
    workspace.
    """
    from prokube.cli.mcp_server import create_server  # the MCP SDK loads slowly

    url, workspace = _resolve(url, workspace)
    if auth.workload_token_file() is None:
        url, workspace = _require(url, workspace)
    server = create_server(lambda: AsyncSandboxClient(url, workspace))
    if port:
        server.run("streamable-http", host=host, port=port, stateless_http=True)
    else:
        server.run()


# --- Presets ---------------------------------------------------------------


@cli.group()
def preset() -> None:
    """Show the platform's sandbox presets."""


@preset.command("list")
@_target_options
def preset_list(url: str | None, workspace: str | None) -> None:
    """List the presets; the default one is marked with *."""
    presets = _with_client(url, workspace, lambda client: client.presets())
    rows = [("NAME", "SIZE", "DESCRIPTION")]
    for p in presets.presets:
        name = f"{p.name} *" if p.name == presets.default else p.name
        description = p.title + (f": {p.description}" if p.description else "")
        rows.append((name, p.size, description))
    _table(rows)


# --- Helpers ---------------------------------------------------------------


def _connect(
    name: str,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
    install_ssh: bool = False,
) -> None:
    alias, config_file = _prepare_ssh(name, identity, url, workspace)
    if install_ssh:
        _install_ssh(alias)
        sys.stdout.flush()  # exec does not flush Python's buffers
    os.execvp("ssh", ["ssh", "-F", str(config_file), alias])


def _install_ssh(alias: str | None) -> None:
    """Include the host entries from ~/.ssh/config and name the sandbox's host."""
    path, changed = ssh.install_include()
    if changed:
        click.echo(f'Added Include "{ssh.include_pattern()}" to {path}')
    else:
        click.echo(f"{path} already includes the pk ssh hosts")
    if alias:
        click.echo(
            f"SSH host: {alias} (ssh {alias}, or pick it in VS Code Remote-SSH "
            "or JetBrains Gateway)"
        )


def _prepare_ssh(
    name: str,
    identity: Path | None,
    url: str | None,
    workspace: str | None,
    resume: bool | None = None,
) -> tuple[str, Path]:
    """Install the user's key, pin the host key and write the ssh config entry.

    resume None keeps whether the existing entry resumes a suspended sandbox.
    """
    url, workspace = _require(url, workspace)
    identity = identity or ssh.default_identity()
    if identity is None:
        raise click.ClickException(
            "No SSH key in ~/.ssh. Create one with ssh-keygen -t ed25519 "
            "or pass --identity."
        )
    public_key = identity.with_name(identity.name + ".pub").read_text()

    async def install_key(client: AsyncSandboxClient) -> str:
        host_key = await _host_key(client, name)
        current = (await client.read_file(name, ssh.AUTHORIZED_KEYS_PATH)).decode()
        updated = ssh.add_authorized_key(current, public_key)
        if updated != current:
            await client.write_file(name, ssh.AUTHORIZED_KEYS_PATH, updated)
        return host_key

    host_key = _with_env_guest(url, workspace, name, install_key)
    alias = ssh.host_alias(workspace, actor_name(name).removeprefix(SANDBOX_PREFIX))
    ssh.pin_host_key(alias, host_key)
    if resume is None:
        resume = ssh.resumes(alias)
    proxy_command = [sys.executable, "-m", "prokube.cli", "sandbox", "proxy", name]
    proxy_command += ["--url", url, "--workspace", workspace]
    if resume:
        proxy_command.append("--resume")
    return alias, ssh.write_host_config(alias, proxy_command, identity)


async def _host_key(client: AsyncSandboxClient, name: str) -> str:
    """The sandbox's SSH host public key, as pk-sandbox-hostkey prints it.

    Falls back to the key file on images without that command.
    """
    result = await client.run(name, ssh.HOST_KEY_COMMAND)
    if result.exit_code == 127:  # sh: command not found
        return (await client.read_file(name, ssh.HOST_KEY_PATH)).decode()
    if result.exit_code != 0 or len(result.stdout.split()) < 2:
        raise click.ClickException(
            f"{name}: {ssh.HOST_KEY_COMMAND} failed (exit {result.exit_code}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


async def _listens(client: AsyncSandboxClient, name: str, port: int) -> bool:
    """Whether something listens on port in the sandbox.

    True when the sandbox shows no listening ports at all: then it cannot tell.
    """
    result = await client.run(name, "cat /proc/net/tcp /proc/net/tcp6")
    ports = ssh.listening_ports(result.stdout)
    return not ports or port in ports


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


def _with_env_guest(
    url: str | None,
    workspace: str | None,
    name: str,
    action: Callable[[AsyncSandboxClient], Awaitable[T]],
) -> T:
    """Like _with_client, for calls to the sandbox's env guest.

    When one fails, says so if the sandbox's image has no env guest.
    """

    async def run(client: AsyncSandboxClient) -> T:
        try:
            return await action(client)
        except EnvError as error:
            try:
                env_guest = await client.has_env_guest(name)
            except EnvError:
                env_guest = True  # cannot tell; show the original error
            if env_guest:
                raise
            raise click.ClickException(
                f"{name}: this sandbox's image has no env guest (not based on "
                "pk-sandbox-coding), so exec, cp and SSH do not work in it."
            ) from error

    return _with_client(url, workspace, run)


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


def _credential(url: str) -> tuple[str, str]:
    """The credential header: ``$PROKUBE_API_KEY``, else the login's token."""
    try:
        return auth.credential(url)()
    except auth.LoginRequired as error:
        raise click.ClickException(
            f"Not logged in to {url}. Run: pk login {url}"
        ) from error


def _resolve(url: str | None, workspace: str | None) -> tuple[str | None, str | None]:
    """Fill in url and workspace from $PROKUBE_URL, $PROKUBE_WORKSPACE, then the saved config."""
    saved = auth.load_config()
    url = url or os.environ.get("PROKUBE_URL") or saved.get("url")
    workspace = (
        workspace or os.environ.get("PROKUBE_WORKSPACE") or saved.get("workspace")
    )
    return (_normalize(url) if url else None), workspace


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


def _preset_of(actor: ateapi.Actor, presets: Presets | None) -> tuple[str, str]:
    """The sandbox's preset and size; its template if it is not a preset's."""
    preset = presets.of(actor.actor_template) if presets else None
    if preset:
        return preset.name, preset.size
    ref = actor.actor_template
    return f"{ref.atespace}/{ref.name}", "-"


def _summary(actor: ateapi.Actor, presets: Presets) -> str:
    preset, size = _preset_of(actor, presets)
    return f"{_state(actor)}, preset {preset}, size {size}"


def _table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        click.echo(
            "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip()
        )


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
