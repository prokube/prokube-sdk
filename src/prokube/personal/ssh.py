"""Automatic standard OpenSSH setup and an opaque authenticated WS transport."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from filelock import FileLock
from websockets.sync.client import connect

from .auth import Auth
from .client import check_name
from .config import Profile, config_dir
from .errors import AuthenticationRequired, HostKeyChanged, SandboxError


def _request(profile, token, method, path, **kwargs):
    try:
        response = httpx.request(
            method,
            profile.api_url + path,
            headers={"Authorization": "Bearer " + token},
            timeout=90,
            **kwargs,
        )
    except httpx.HTTPError as error:
        raise SandboxError("Cannot reach the Sandbox connection endpoint.") from error
    if response.status_code == 401:
        raise AuthenticationRequired("Session expired. Run pk login.")
    if response.status_code >= 400:
        raise SandboxError(f"Sandbox connection refused (HTTP {response.status_code}).")
    return response.json()


def prepare(
    profile: Profile,
    name: str,
    identity_file: str | None = None,
    *,
    expected_subject=None,
) -> Path:
    check_name(name)
    auth = Auth(profile)
    token = auth.token()
    me = _request(profile, token, "GET", "/v1/me")
    if expected_subject is not None and me["subject"] != expected_subject:
        raise AuthenticationRequired(
            "Identity changed during connection setup. Retry with the intended profile."
        )
    account = hashlib.sha256(
        (profile.fingerprint + me["subject"]).encode()
    ).hexdigest()[:24]
    root = config_dir() / "ssh" / account
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(root / "setup.lock"), timeout=30):
        standard = Path.home() / ".ssh/id_ed25519"
        key = (
            Path(identity_file).expanduser().resolve()
            if identity_file
            else standard
            if standard.exists()
            else root / "id_ed25519"
        )
        if not key.exists():
            if identity_file:
                raise SandboxError("Selected SSH identity file does not exist.")
            generated = Ed25519PrivateKey.generate()
            with os.fdopen(
                os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as stream:
                stream.write(
                    generated.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.OpenSSH,
                        serialization.NoEncryption(),
                    )
                )
            public_bytes = generated.public_key().public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            )
            Path(str(key) + ".pub").write_bytes(public_bytes + b"\n")
        public = Path(str(key) + ".pub")
        if not public.exists():
            raise SandboxError(
                "SSH public key file is missing next to the private key."
            )
        result = _request(
            profile,
            token,
            "PUT",
            f"/v1/sandboxes/{name}/ssh-key",
            json={"publicKey": public.read_text()},
        )
        directory = root / name
        directory.mkdir(exist_ok=True, mode=0o700)
        alias = "prokube-" + name
        host = alias + " " + result["hostPublicKey"] + "\n"
        known = directory / "known_hosts"
        if known.exists() and known.read_text().split()[:3] != host.split()[:3]:
            raise HostKeyChanged(
                f"SSH host identity changed. Verify the rotation with your platform operator before replacing {known}."
            )
        known.write_text(host)
        known.chmod(0o600)
        command = shlex.join(
            [
                sys.executable,
                "-m",
                "prokube.personal.cli",
                "--profile",
                profile.name,
                "_ssh-proxy",
                name,
                "--subject",
                me["subject"],
            ]
        )

        def quote(path):
            value = str(path)
            if "\n" in value or "\r" in value:
                raise SandboxError("Unsupported newline in SSH configuration path.")
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        cfg = directory / "config"
        cfg.write_text(f"""Host {alias}
  HostName {alias}
  User developer
  IdentityFile {quote(key)}
  UserKnownHostsFile {quote(known)}
  GlobalKnownHostsFile /dev/null
  StrictHostKeyChecking yes
  IdentitiesOnly yes
  ControlMaster no
  ControlPath none
  ConnectTimeout 30
  ServerAliveInterval 20
  ProxyCommand {command}
""")
        cfg.chmod(0o600)
        return cfg


def terminal(
    profile: Profile,
    name: str,
    identity_file=None,
    *,
    replace_process=False,
    expected_subject=None,
) -> int:
    config = prepare(profile, name, identity_file, expected_subject=expected_subject)
    args = [
        "ssh",
        "-F",
        str(config),
        "-t",
        "prokube-" + name,
        "tmux new-session -A -s workspace",
    ]
    if replace_process:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvp("ssh", args)
    return subprocess.call(args)


def proxy(profile: Profile, name: str, expected_subject: str) -> None:
    check_name(name)
    token = Auth(profile).token()
    if _request(profile, token, "GET", "/v1/me")["subject"] != expected_subject:
        raise AuthenticationRequired(
            "This SSH configuration belongs to a different identity. Run pk sandbox ssh-config again."
        )
    url = (
        profile.api_url.replace("https://", "wss://", 1)
        + f"/v1/sandboxes/{name}/ssh/ws"
    )
    if urlparse(url).scheme != "wss":
        raise SandboxError("SSH requires WSS.")
    try:
        with connect(
            url,
            additional_headers={"Authorization": "Bearer " + token},
            open_timeout=90,
            max_size=1024 * 1024,
        ) as ws:

            def send():
                try:
                    while True:
                        data = os.read(sys.stdin.fileno(), 65536)
                        if not data:
                            break
                        ws.send(data)
                except Exception:
                    pass
                finally:
                    ws.close()

            threading.Thread(target=send, daemon=True).start()
            for data in ws:
                if not isinstance(data, bytes):
                    raise SandboxError("Unexpected non-binary SSH tunnel data.")
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
    except BrokenPipeError:
        return
    except Exception as error:
        # Library exceptions can include handshake metadata. Never print them.
        raise SandboxError(
            "SSH tunnel closed or was refused; check your login and Sandbox access."
        ) from error
