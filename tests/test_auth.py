import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

import keyring
import pytest
from keyring.backends import fail

from prokube import auth, endpoints
from tests.conftest import make_jwt

URL = "https://platform.example.com"
ISSUER = f"{URL}/dex"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
TOKEN_ENDPOINT = f"{ISSUER}/token"


@pytest.fixture
def dex(httpx_mock):
    httpx_mock.add_response(
        url=DISCOVERY,
        json={
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/auth",
            "token_endpoint": TOKEN_ENDPOINT,
        },
        is_reusable=True,
    )
    return httpx_mock


@pytest.fixture
def free_redirect(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    redirect = f"http://localhost:{port}/callback"
    monkeypatch.setattr(endpoints, "REDIRECT_URIS", (redirect,))
    return redirect


def fake_browser(monkeypatch, answer):
    """Replace the browser with a thread that follows the Dex redirect.

    answer(auth_params) returns the query parameters sent to the callback.
    """
    seen: dict[str, str] = {}

    def open_url(url: str) -> None:
        params = {
            key: values[0] for key, values in parse_qs(urlparse(url).query).items()
        }
        seen.update(params)
        redirect = params["redirect_uri"]
        base = redirect.rsplit("/", 1)[0]

        def follow() -> None:
            try:
                urllib.request.urlopen(f"{base}/favicon.ico", timeout=5)
            except urllib.error.HTTPError:
                pass  # 404; the login must keep waiting for the callback
            urllib.request.urlopen(f"{redirect}?{urlencode(answer(params))}", timeout=5)

        threading.Thread(target=follow, daemon=True).start()

    monkeypatch.setattr(auth.webbrowser, "open", open_url)
    return seen


def stored(url: str = URL) -> dict:
    return json.loads(keyring.get_password(auth.KEYRING_SERVICE, url) or "null")


def test_pkce_challenge_matches_rfc_7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert (
        auth._pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_login_runs_pkce_flow_and_stores_tokens(monkeypatch, dex, free_redirect):
    id_token = make_jwt(time.time() + 3600)
    dex.add_response(
        url=TOKEN_ENDPOINT,
        method="POST",
        json={"id_token": id_token, "refresh_token": "refresh-1"},
    )
    seen = fake_browser(
        monkeypatch, lambda p: {"code": "the-code", "state": p["state"]}
    )

    session = auth.login(URL + "/")

    assert seen["client_id"] == "prokube-cli"
    assert seen["redirect_uri"] == free_redirect
    assert seen["code_challenge_method"] == "S256"
    assert "offline_access" in seen["scope"].split()
    form = parse_qs(dex.get_request(method="POST").content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["the-code"]
    assert form["redirect_uri"] == [free_redirect]
    assert auth._pkce_challenge(form["code_verifier"][0]) == seen["code_challenge"]
    assert stored()["refresh_token"] == "refresh-1"
    assert session.token() == id_token


def test_login_rejects_a_foreign_state(monkeypatch, dex, free_redirect):
    fake_browser(monkeypatch, lambda p: {"code": "the-code", "state": "forged"})

    with pytest.raises(auth.LoginError, match="state"):
        auth.login(URL)
    assert stored() is None


def test_login_reports_dex_errors(monkeypatch, dex, free_redirect):
    fake_browser(
        monkeypatch,
        lambda p: {
            "error": "access_denied",
            "error_description": "no",
            "state": p["state"],
        },
    )

    with pytest.raises(auth.LoginError, match="no"):
        auth.login(URL)


def test_session_requires_a_login():
    with pytest.raises(auth.LoginRequired, match="login"):
        auth.LoginSession(URL)


def test_token_is_reused_while_valid():
    tokens = {"id_token": make_jwt(time.time() + 3600), "refresh_token": "r"}
    auth._save_tokens(URL, tokens | {"expires_at": time.time() + 3600})

    # pytest-httpx would fail the test on any HTTP request.
    assert auth.LoginSession(URL).token() == tokens["id_token"]


def test_token_refreshes_and_stores_the_rotated_refresh_token(dex):
    auth._save_tokens(
        URL,
        {"id_token": "old", "refresh_token": "refresh-1", "expires_at": time.time()},
    )
    new_token = make_jwt(time.time() + 3600)
    dex.add_response(
        url=TOKEN_ENDPOINT,
        method="POST",
        json={"id_token": new_token, "refresh_token": "refresh-2"},
    )

    assert auth.LoginSession(URL).token() == new_token

    form = parse_qs(dex.get_request(method="POST").content.decode())
    assert form == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-1"],
        "client_id": ["prokube-cli"],
    }
    assert stored()["refresh_token"] == "refresh-2"


def test_rejected_refresh_requires_a_new_login(dex):
    auth._save_tokens(
        URL, {"id_token": "old", "refresh_token": "spent", "expires_at": time.time()}
    )
    dex.add_response(url=TOKEN_ENDPOINT, method="POST", status_code=400)

    with pytest.raises(auth.LoginRequired, match="expired"):
        auth.LoginSession(URL).token()


def test_tokens_go_to_a_private_file_without_keyring(tmp_path):
    keyring.set_keyring(fail.Keyring())
    tokens = {"id_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600}

    auth._save_tokens(URL, tokens)

    path = tmp_path / "config" / "tokens" / "platform.example.com.json"
    assert json.loads(path.read_text()) == tokens
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert auth.LoginSession(URL).token() == "t"


def test_logout_forgets_keyring_and_file(memory_keyring):
    tokens = {"id_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600}
    auth._save_tokens(URL, tokens)
    keyring.set_keyring(fail.Keyring())
    auth._save_tokens(URL, tokens)
    keyring.set_keyring(memory_keyring)

    auth.logout(URL)

    assert auth._load_tokens(URL) is None
    auth.logout(URL)  # nothing left to forget


def test_platform_url_must_be_https():
    with pytest.raises(ValueError, match="https"):
        auth.login("http://platform.example.com")


def test_endpoints_derive_from_the_platform_url():
    assert endpoints.dex_issuer(URL + "/") == ISSUER
    assert endpoints.ate_api_target(URL) == "platform.example.com:443"
    assert endpoints.env_target("https://platform.example.com:8443") == (
        "platform.example.com:8443"
    )


def test_token_file_is_read_again_only_before_expiry(tmp_path):
    path = tmp_path / "token"
    lasting, expiring, renewed = (
        make_jwt(time.time() + 3600),
        make_jwt(time.time() + 30),
        make_jwt(time.time() + 3600),
    )
    path.write_text(lasting + "\n")
    token_file = auth.TokenFile(path)
    assert token_file.token() == lasting
    path.write_text(expiring)
    assert token_file.token() == lasting  # still valid, no read

    token_file = auth.TokenFile(path)
    assert token_file.token() == expiring
    path.write_text(renewed)  # the kubelet rotated it
    assert token_file.token() == renewed


def test_workload_token_file_path_is_configurable(tmp_path, monkeypatch):
    path = tmp_path / "svid.jwt"
    assert auth.workload_token_file() is None
    path.write_text(make_jwt(time.time() + 3600))
    monkeypatch.setenv("PROKUBE_SANDBOX_TOKEN_FILE", str(path))
    assert auth.workload_token_file() == path


def test_default_token_provider_prefers_the_login(tmp_path, monkeypatch):
    workload = make_jwt(time.time() + 3600)
    path = tmp_path / "token"
    path.write_text(workload)

    with pytest.raises(auth.LoginRequired):
        auth.default_token_provider(URL)
    with pytest.raises(auth.LoginRequired):
        auth.default_token_provider(None)

    monkeypatch.setenv("PROKUBE_SANDBOX_TOKEN_FILE", str(path))
    assert auth.default_token_provider(URL)() == workload
    assert auth.default_token_provider(None)() == workload

    user = make_jwt(time.time() + 3600)
    auth._save_tokens(URL, {"id_token": user, "expires_at": time.time() + 3600})
    assert auth.default_token_provider(URL)() == user
