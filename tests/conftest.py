import base64
import json

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.passwords.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def memory_keyring(tmp_path, monkeypatch):
    """Keep every test away from the real keyring and ~/.config."""
    monkeypatch.setenv("PROKUBE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("PROKUBE_URL", raising=False)
    monkeypatch.delenv("PROKUBE_WORKSPACE", raising=False)
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def make_jwt(exp: float) -> str:
    """An unsigned JWT with only an exp claim; the SDK never verifies it."""

    def encode(value: dict) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': exp, 'sub': 'x'})}.sig"
