"""Public profiles and explicit credential storage; never silently use plaintext."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import keyring
from filelock import FileLock
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError

from .errors import AuthenticationRequired, ConnectionFailed, SandboxError


def config_dir() -> Path:
    return Path(os.environ.get("PROKUBE_CONFIG_DIR", Path.home() / ".config/prokube"))


def private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pk-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Profile:
    name: str
    platform_url: str
    api_url: str
    grpc_target: str
    issuer: str
    client_id: str
    audiences: list[str]
    redirect_uris: list[str]
    credential_store: str = "keyring"

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", self.name):
            raise SandboxError("Invalid profile name.")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


class Profiles:
    def __init__(self, root: Path | None = None):
        self.root = root or config_dir()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "config.json"
        self.lock = FileLock(str(self.root / "profiles.lock"), timeout=10)

    def read(self) -> dict:
        return (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"profiles": {}, "active_profile": "default"}
        )

    def load(self, name: str | None = None) -> Profile:
        config = self.read()
        name = name or config["active_profile"]
        if name not in config["profiles"]:
            raise AuthenticationRequired(
                "No platform profile. Run: pk login <platform-url>"
            )
        return Profile(**config["profiles"][name])

    def save(self, profile: Profile) -> None:
        with self.lock:
            config = self.read()
            config["profiles"][profile.name] = asdict(profile)
            config["active_profile"] = profile.name
            private_write(self.path, config)


def discover(platform: str, name: str = "default", store: str = "keyring") -> Profile:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", name):
        raise SandboxError(
            "Profile name must contain only letters, digits, '-' or '_'."
        )
    parsed = urlparse(platform)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise SandboxError(
            "Use an HTTPS platform URL without credentials or query parameters."
        )
    base = platform.rstrip("/")
    if not parsed.path.strip("/"):
        base += "/sandbox-mvp"
    try:
        response = httpx.get(base + "/.well-known/client", timeout=15)
        response.raise_for_status()
        info = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ConnectionFailed(
            "Cannot read Sandbox client configuration from this platform."
        ) from error
    target = urlparse(info["api_url"])
    issuer = urlparse(info["issuer"])
    if (
        target.scheme != "https"
        or target.netloc != parsed.netloc
        or issuer.scheme != "https"
        or issuer.username
    ):
        raise SandboxError("Platform returned an invalid HTTPS client configuration.")
    if info["grpc_target"] not in (target.netloc, target.netloc + ":443"):
        raise SandboxError("gRPC endpoint must use the platform's authority.")
    if store not in ("keyring", "file"):
        raise SandboxError(
            "Credential store must be keyring or explicitly selected file storage."
        )
    return Profile(
        name,
        platform.rstrip("/"),
        info["api_url"],
        info["grpc_target"],
        info["issuer"],
        info["client_id"],
        info["audiences"],
        info["redirect_uris"],
        store,
    )


class Credentials:
    def __init__(self, profile: Profile, root: Path | None = None):
        self.profile = profile
        self.root = root or config_dir()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = FileLock(
            str(self.root / f"{profile.name}.credentials.lock"), timeout=15
        )
        self.path = self.root / f"{profile.name}.credentials.json"
        self.account = "profile-" + profile.name

    def _keyring(self):
        backend = keyring.get_keyring()
        if isinstance(backend, ChainerBackend):
            candidates = list(backend.backends)
            backend = candidates[0] if candidates else backend
        if not type(backend).__module__.startswith(
            (
                "keyring.backends.macOS",
                "keyring.backends.Windows",
                "keyring.backends.SecretService",
                "keyring.backends.kwallet",
            )
        ):
            raise AuthenticationRequired(
                "No supported secure keyring. Choose file storage explicitly with pk login --credential-store=file."
            )
        return backend

    def read(self) -> dict | None:
        if self.profile.credential_store == "file":
            if not self.path.exists():
                return None
            if self.path.stat().st_mode & 0o077:
                raise AuthenticationRequired(
                    "Credential file permissions must be 0600."
                )
            raw = self.path.read_text()
        else:
            raw = self._call_keyring("get_password", "prokube-sdk", self.account)
        value = json.loads(raw) if raw else None
        if value and value.get("profile") != self.profile.fingerprint:
            raise AuthenticationRequired(
                "Platform settings changed. Run pk login again."
            )
        return value

    def save(self, value: dict) -> None:
        value = dict(value, profile=self.profile.fingerprint)
        if self.profile.credential_store == "file":
            private_write(self.path, value)
        else:
            self._call_keyring(
                "set_password", "prokube-sdk", self.account, json.dumps(value)
            )

    def clear(self) -> None:
        with self.lock:
            if self.profile.credential_store == "file":
                self.path.unlink(missing_ok=True)
            elif self._call_keyring("get_password", "prokube-sdk", self.account):
                self._call_keyring("delete_password", "prokube-sdk", self.account)

    def _call_keyring(self, method, *args):
        try:
            return getattr(self._keyring(), method)(*args)
        except KeyringError as error:
            raise AuthenticationRequired(
                "Cannot access the system keyring. Unlock it or explicitly select file storage."
            ) from error
