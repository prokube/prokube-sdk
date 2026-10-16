import base64
import hashlib
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from prokube.personal.auth import Auth
from prokube.personal.config import Credentials, Profile, Profiles
from prokube.personal.errors import AuthenticationRequired, SandboxError


def profile(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return Profile(
        "test",
        "https://platform.invalid",
        "https://platform.invalid/sandbox-mvp",
        "platform.invalid",
        "https://issuer.invalid",
        "prokube-cli",
        ["prokube-cli", "oauth2-oidc-client"],
        [f"http://localhost:{port}/callback"],
        "file",
    )


@pytest.fixture
def oidc(tmp_path, monkeypatch):
    p = profile(tmp_path)
    a = Auth(p, Credentials(p, tmp_path))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        a,
        "_keys",
        SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
        ),
    )
    monkeypatch.setattr(
        a,
        "metadata",
        lambda: {
            "authorization_endpoint": "https://issuer.invalid/auth",
            "token_endpoint": "https://issuer.invalid/token",
        },
    )

    def token(**overrides):
        claims = {
            "iss": p.issuer,
            "sub": "stable-user",
            "aud": p.client_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256")

    return a, token


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "other"},
        {"exp": 1},
        {"iat": int(time.time()) + 10000},
    ],
)
def test_token_validation(oidc, changes):
    a, token = oidc
    with pytest.raises(AuthenticationRequired):
        a.verify(token(**changes))


def test_nonce_and_signature(oidc):
    a, token = oidc
    assert (
        a.verify(token(nonce="expected"), login=True, nonce="expected")["sub"]
        == "stable-user"
    )
    with pytest.raises(AuthenticationRequired):
        a.verify(token(nonce="wrong"), login=True, nonce="expected")
    forged = token().split(".")
    forged[1] = base64.urlsafe_b64encode(b'{"sub":"other"}').decode().rstrip("=")
    with pytest.raises(AuthenticationRequired):
        a.verify(".".join(forged))


def test_explicit_file_storage_profile_binding_and_identity_latch(oidc):
    a, token = oidc
    p = a.profile
    a.credentials.save(
        {"subject": "stable-user", "expires": time.time() + 600, "id_token": token()}
    )
    assert a.credentials.path.stat().st_mode & 0o777 == 0o600
    a.token()
    a.credentials.save(
        {
            "subject": "other-user",
            "expires": time.time() + 600,
            "id_token": token(sub="other-user"),
        }
    )
    with pytest.raises(AuthenticationRequired, match="identity changed"):
        a.token()
    changed = Profile(**{**p.__dict__, "api_url": "https://another.invalid"})
    with pytest.raises(AuthenticationRequired, match="settings changed"):
        Credentials(changed, a.credentials.root).read()


def test_refresh_serializes_and_preserves_identity(oidc, monkeypatch):
    a, token = oidc
    a.credentials.save(
        {
            "subject": "stable-user",
            "expires": 1,
            "id_token": token(),
            "refresh_token": "old",
            "nonce": "n",
        }
    )
    calls = []

    @contextmanager
    def client(**options):
        def refresh(endpoint, refresh_token):
            calls.append(refresh_token)
            time.sleep(0.1)
            return {"id_token": token(nonce="n"), "refresh_token": "rotated"}

        yield SimpleNamespace(refresh_token=refresh)

    monkeypatch.setattr("prokube.personal.auth.oauth_client", client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: a.token(), range(2)))
    assert results[0] == results[1]
    assert calls == ["old"]
    assert a.credentials.read()["refresh_token"] == "rotated"


def test_callback_state_and_pkce(oidc, monkeypatch):
    a, token = oidc
    announced = []

    def announce(message):
        announced.append(message.splitlines()[-1])

    monkeypatch.setattr(a, "check_access", lambda token, claims: None)

    def exchange(self, endpoint, **options):
        params = parse_qs(urlparse(announced[0]).query)
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(options["code_verifier"].encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        assert params["code_challenge"] == [challenge]
        assert params["code_challenge_method"] == ["S256"]
        return {"id_token": token(nonce=params["nonce"][0]), "refresh_token": "refresh"}

    monkeypatch.setattr("prokube.personal.auth.OAuth2Client.fetch_token", exchange)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(a.login, announce=announce, open_browser=False, timeout=5)
        deadline = time.monotonic() + 3
        while not announced and time.monotonic() < deadline:
            time.sleep(0.01)
        assert announced
        params = parse_qs(urlparse(announced[0]).query)
        callback = params["redirect_uri"][0]
        assert (
            httpx.get(callback, params={"state": "wrong", "code": "code"}).status_code
            == 400
        )
        assert not future.done()
        assert (
            httpx.get(
                callback, params={"state": params["state"][0], "code": "code"}
            ).status_code
            == 200
        )
        assert future.result(timeout=3)["sub"] == "stable-user"
    assert a.credentials.read()["subject"] == "stable-user"


def test_profiles_do_not_store_tokens(tmp_path):
    p = profile(tmp_path)
    profiles = Profiles(tmp_path)
    profiles.save(p)
    assert profiles.load() == p
    assert "id_token" not in profiles.path.read_text()
    with pytest.raises(SandboxError):
        Profile(**{**p.__dict__, "name": "../escape"})
