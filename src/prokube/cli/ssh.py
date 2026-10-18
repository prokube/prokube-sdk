"""OpenSSH to sandboxes over a WebSocket.

The runtime image (prokube/sandbox-images#16) bridges a WebSocket on port 8022,
path ``/ssh``, to its sshd. sshd accepts public keys for the user ``developer``
from ``/workspace/ssh/authorized_keys`` and keeps its host key in
``/workspace/ssh/``. The CLI reads and writes both through the authenticated env
file API, so the host key it pins is the sandbox's real key, not one learned on
first use.

The ssh files live under ``<config dir>/ssh/``: one private ``known_hosts`` and
one config file per sandbox in ``hosts/``. ``install_include`` puts one
``Include`` of ``hosts/*.conf`` at the top of ``~/.ssh/config``, so ssh and IDEs
find every sandbox.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tempfile
import threading
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from prokube import auth

SSH_USER = "developer"
HOST_KEY_PATH = "/workspace/ssh/ssh_host_ed25519_key.pub"
# Prints the sandbox's own host public key, generating it first in a clone of a
# golden snapshot (prokube/sandbox-images#21). Older images only have the file.
HOST_KEY_COMMAND = "/usr/local/bin/pk-sandbox-hostkey"
AUTHORIZED_KEYS_PATH = "/workspace/ssh/authorized_keys"
DEFAULT_IDENTITIES = ("id_ed25519", "id_ecdsa", "id_rsa")
INCLUDE_MARKER = "# added by pk"
# An ssh_config line: keyword, then whitespace or "=", then the arguments.
_CONFIG_LINE = re.compile(r"\s*(\w+)(?:\s*=\s*|\s+)(.*)")


def ssh_dir() -> Path:
    return auth.config_dir() / "ssh"


def known_hosts_file() -> Path:
    return ssh_dir() / "known_hosts"


def host_config_file(alias: str) -> Path:
    return ssh_dir() / "hosts" / f"{alias}.conf"


def user_config_file() -> Path:
    return Path.home() / ".ssh" / "config"


def include_pattern() -> str:
    """The glob that matches every host config file."""
    return str(ssh_dir().absolute() / "hosts" / "*.conf")


def host_alias(workspace: str, name: str) -> str:
    """The ssh host name of a sandbox, e.g. ``pk.team-a.demo``."""
    return f"pk.{workspace}.{name}"


def default_identity() -> Path | None:
    """The user's first default private key that has a .pub next to it."""
    for name in DEFAULT_IDENTITIES:
        path = Path.home() / ".ssh" / name
        if path.with_name(name + ".pub").exists():
            return path
    return None


def add_authorized_key(authorized_keys: str, public_key: str) -> str:
    """Return authorized_keys with public_key appended unless it is already there."""
    key = " ".join(public_key.split()[:2])  # type and base64, without the comment
    if any(key in line for line in authorized_keys.splitlines()):
        return authorized_keys
    if authorized_keys and not authorized_keys.endswith("\n"):
        authorized_keys += "\n"
    return authorized_keys + public_key.strip() + "\n"


def pin_host_key(alias: str, public_key: str) -> None:
    """Make public_key the only known host key of alias."""
    key_type, key = public_key.split()[:2]
    lines = [*_other_hosts(alias), f"{alias} {key_type} {key}"]
    _write_private(known_hosts_file(), "\n".join(lines) + "\n")


def forget_host(alias: str) -> None:
    """Remove the host key and config entry of alias."""
    if known_hosts_file().exists():
        lines = _other_hosts(alias)
        _write_private(known_hosts_file(), "".join(line + "\n" for line in lines))
    host_config_file(alias).unlink(missing_ok=True)


def write_host_config(alias: str, proxy_command: list[str], identity: Path) -> Path:
    """Write the ssh config entry for alias and return its path."""
    lines = [
        f"Host {alias}",
        f"  User {SSH_USER}",
        f"  ProxyCommand {_escape(shlex.join(proxy_command))}",
        f"  HostKeyAlias {alias}",
        f'  UserKnownHostsFile "{_escape(str(known_hosts_file()))}"',
        "  StrictHostKeyChecking yes",
        f'  IdentityFile "{_escape(str(identity))}"',
        "  IdentitiesOnly yes",
        "  ServerAliveInterval 30",
    ]
    path = host_config_file(alias)
    _write_private(path, "\n".join(lines) + "\n")
    return path


def install_include() -> tuple[Path, bool]:
    """Include the host config files from ~/.ssh/config.

    The Include goes at the top: further down, inside a Host or Match block,
    OpenSSH would only apply it to that block. Returns the path of ~/.ssh/config
    and whether it changed.
    """
    path = user_config_file()
    text = path.read_text() if path.exists() else ""
    if _includes(text, include_pattern()):
        return path, False
    header = f'{INCLUDE_MARKER}\nInclude "{include_pattern()}"\n'
    if path.exists():
        _rewrite(path, header + ("\n" + text if text.strip() else ""))
    else:
        _write_private(path, header)  # also creates ~/.ssh with mode 0700
    return path, True


def proxy(url: str, token: str, stdin: int = 0, stdout: int = 1) -> None:
    """Pump bytes between the file descriptors and the WebSocket at url.

    Returns when either side closes. This is what OpenSSH runs as ProxyCommand.
    """
    headers = {"Authorization": f"Bearer {token}"}
    # SSH traffic is encrypted, so compression would only cost CPU.
    with connect(url, additional_headers=headers, compression=None) as websocket:

        def upload() -> None:
            try:
                while data := os.read(stdin, 65536):
                    websocket.send(data)
            except ConnectionClosed:
                pass
            finally:
                websocket.close()

        threading.Thread(target=upload, daemon=True).start()
        try:
            for message in websocket:
                _write_all(
                    stdout, message.encode() if isinstance(message, str) else message
                )
        except ConnectionClosed:
            pass  # the sandbox side went away; ssh reports it


def _other_hosts(alias: str) -> list[str]:
    path = known_hosts_file()
    lines = path.read_text().splitlines() if path.exists() else []
    return [line for line in lines if line.split(" ", 1)[0] != alias]


def _includes(text: str, pattern: str) -> bool:
    """Whether the ssh config text includes pattern before its first block."""
    for line in text.splitlines():
        match = _CONFIG_LINE.match(line)  # comments and blank lines do not match
        if not match:
            continue
        keyword = match[1].lower()
        if keyword in ("host", "match"):
            return False
        if keyword == "include":
            try:
                paths = shlex.split(match[2], comments=True)
            except ValueError:
                continue
            if pattern in (os.path.expanduser(path) for path in paths):
                return True
    return False


def _escape(value: str) -> str:
    # ssh expands %-tokens in these options.
    return value.replace("%", "%%")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _rewrite(path: Path, text: str) -> None:
    """Replace the content of path atomically, keeping its mode and symlinks."""
    target = path.resolve()
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError:
        os.unlink(temporary)
        raise


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)
