import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

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
