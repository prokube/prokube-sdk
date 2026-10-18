import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from prokube import endpoints
from prokube.cli import ssh


@contextmanager
def websocket_server(handler: Callable[[ServerConnection], None]) -> Iterator[str]:
    """A local WebSocket server standing in for the sandbox's websocat."""
    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"ws://127.0.0.1:{server.socket.getsockname()[1]}/ssh"
        server.shutdown()


def run_proxy(url: str, stdin: int, stdout: int) -> None:
    thread = threading.Thread(target=ssh.proxy, args=(url, "tok", stdin, stdout))
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "proxy did not return"


def test_proxy_pumps_bytes_both_ways_and_sends_the_token(tmp_path):
    seen = {}
    reply = os.urandom(300_000)  # more than one read, frame and pipe buffer

    def handler(websocket: ServerConnection) -> None:
        assert websocket.request is not None
        seen["path"] = websocket.request.path
        seen["authorization"] = websocket.request.headers["Authorization"]
        seen["data"] = websocket.recv()
        for start in range(0, len(reply), 65536):
            websocket.send(reply[start : start + 65536])
        websocket.close()

    stdin_read, stdin_write = os.pipe()
    os.write(stdin_write, b"SSH-2.0-OpenSSH\r\n")
    with (tmp_path / "stdout").open("wb") as stdout:
        with websocket_server(handler) as url:
            run_proxy(url, stdin_read, stdout.fileno())
    os.close(stdin_write)  # lets the upload thread finish

    assert seen == {
        "path": "/ssh",
        "authorization": "Bearer tok",
        "data": b"SSH-2.0-OpenSSH\r\n",
    }
    assert (tmp_path / "stdout").read_bytes() == reply


def test_proxy_closes_the_websocket_when_stdin_ends(tmp_path):
    received = bytearray()

    def handler(websocket: ServerConnection) -> None:
        try:
            for message in websocket:
                assert isinstance(message, bytes)  # binary frames for websocat
                received.extend(message)
        except ConnectionClosed:
            pass

    stdin_read, stdin_write = os.pipe()
    os.write(stdin_write, b"bye")
    os.close(stdin_write)
    with (tmp_path / "stdout").open("wb") as stdout:
        with websocket_server(handler) as url:
            run_proxy(url, stdin_read, stdout.fileno())

    assert received == b"bye"


def test_ssh_url_goes_through_the_env_port_proxy():
    url = endpoints.ssh_url("https://platform.example.com/", "team-a", "sbx-demo")

    assert url == (
        "wss://platform.example.com/svc/sandbox/v1alpha/atespaces/team-a"
        "/envs/sbx-demo/ports/8022/ssh"
    )


def test_pin_host_key_replaces_only_that_hosts_key():
    ssh.pin_host_key("pk.team-a.one", "ssh-ed25519 AAAAone root@one\n")
    ssh.pin_host_key("pk.team-a.two", "ssh-ed25519 AAAAold root@two\n")
    ssh.pin_host_key("pk.team-a.two", "ssh-ed25519 AAAAnew root@two\n")

    path = ssh.known_hosts_file()
    assert path.read_text() == (
        "pk.team-a.one ssh-ed25519 AAAAone\npk.team-a.two ssh-ed25519 AAAAnew\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    ssh.forget_host("pk.team-a.one")
    assert path.read_text() == "pk.team-a.two ssh-ed25519 AAAAnew\n"


def test_add_authorized_key_appends_once():
    key = "ssh-ed25519 AAAAuser me@laptop"

    once = ssh.add_authorized_key("ssh-rsa AAAAother", key)
    assert once == "ssh-rsa AAAAother\nssh-ed25519 AAAAuser me@laptop\n"
    assert ssh.add_authorized_key(once, "ssh-ed25519 AAAAuser other-comment") == once
    assert ssh.add_authorized_key("", key) == key + "\n"


def test_host_config_pins_the_private_known_hosts(tmp_path):
    identity = Path("/home/me/keys/100%/id_ed25519")

    path = ssh.write_host_config("pk.team-a.demo", ["pk", "a b", "%h"], identity)

    assert path.read_text() == (
        "Host pk.team-a.demo\n"
        "  User developer\n"
        "  ProxyCommand pk 'a b' %%h\n"
        "  HostKeyAlias pk.team-a.demo\n"
        f'  UserKnownHostsFile "{ssh.known_hosts_file()}"\n'
        "  StrictHostKeyChecking yes\n"
        '  IdentityFile "/home/me/keys/100%%/id_ed25519"\n'
        "  IdentitiesOnly yes\n"
        "  ServerAliveInterval 30\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- ~/.ssh/config Include ---------------------------------------------------


def include_header(tmp_path: Path) -> str:
    pattern = tmp_path / "config" / "ssh" / "hosts" / "*.conf"  # PROKUBE_CONFIG_DIR
    return f'# added by pk\nInclude "{pattern}"\n'


def test_install_include_creates_ssh_dir_and_config_privately(tmp_path):
    path, changed = ssh.install_include()

    assert (path, changed) == (tmp_path / "home" / ".ssh" / "config", True)
    assert path.read_text() == include_header(tmp_path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_install_include_fills_an_empty_config_and_keeps_its_mode(tmp_path):
    path = ssh.user_config_file()
    path.parent.mkdir(parents=True)
    path.write_text("")
    path.chmod(0o640)

    assert ssh.install_include() == (path, True)
    assert path.read_text() == include_header(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_install_include_goes_above_the_existing_config_once(tmp_path):
    real = tmp_path / "dotfiles" / "ssh_config"
    real.parent.mkdir()
    real.write_text("Host *\n  User me\n")
    real.chmod(0o600)
    path = ssh.user_config_file()
    path.parent.mkdir(parents=True)
    path.symlink_to(real)

    assert ssh.install_include() == (path, True)
    assert ssh.install_include() == (path, False)

    assert path.is_symlink()
    assert real.read_text() == include_header(tmp_path) + "\nHost *\n  User me\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert os.listdir(real.parent) == ["ssh_config"]  # no temporary file left


def test_install_include_recognizes_a_hand_written_include(monkeypatch, tmp_path):
    monkeypatch.delenv("PROKUBE_CONFIG_DIR")  # ~/.config/prokube
    path = ssh.user_config_file()
    path.parent.mkdir(parents=True)
    hand_written = "IdentitiesOnly yes\ninclude ~/.config/prokube/ssh/hosts/*.conf\n"
    path.write_text(hand_written)

    assert ssh.install_include() == (path, False)
    assert path.read_text() == hand_written


def test_install_include_ignores_an_include_inside_a_block():
    path = ssh.user_config_file()
    path.parent.mkdir(parents=True)
    scoped = f'Host work\n  Include "{ssh.include_pattern()}"\n'
    path.write_text(scoped)

    assert ssh.install_include() == (path, True)
    assert path.read_text().startswith("# added by pk\nInclude ")
    assert path.read_text().endswith("\n\n" + scoped)


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client")
def test_openssh_reads_the_host_entry_through_the_include(tmp_path):
    config = ssh.user_config_file()
    config.parent.mkdir(parents=True)
    config.write_text("Host *\n  User someone-else\n")
    ssh.write_host_config("pk.team-a.demo", ["pk", "proxy"], tmp_path / "id")
    ssh.install_include()

    result = subprocess.run(
        ["ssh", "-G", "-F", str(config), "pk.team-a.demo"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "user developer" in result.stdout.splitlines()
    assert "hostkeyalias pk.team-a.demo" in result.stdout.splitlines()
