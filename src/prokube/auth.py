"""Log in to a prokube platform and keep the Dex token fresh.

``login(url)`` runs the OAuth Authorization Code flow with PKCE against the
platform's Dex, using the public client ``prokube-cli`` and a localhost
redirect. The tokens are stored in the OS keyring, or in a private file
(mode 0600) when no keyring is available.

``LoginSession(url).token`` returns a valid ID token and uses the refresh
token shortly before it expires. Dex rotates the refresh token on every use,
so the new one is stored each time.

In a workspace pod, ``TokenFile`` reads the pod's own token instead (workload
identity, prokube/prokube-neo#100). ``default_token_provider`` picks between
the two.

Without a login, a pkui API key with the scope "Sandboxes" works too: it goes
in the ``x-api-key`` header instead of a token. ``credential`` picks the header.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring

from prokube import endpoints

# Returns the bearer token for the next API call. Any callable works, so other
# credentials can be plugged in.
TokenProvider = Callable[[], str]

# Returns the credential header for the next API call: (name, value).
Credential = Callable[[], tuple[str, str]]

# A pkui API key with the scope "Sandboxes" of a workspace.
API_KEY_ENV = "PROKUBE_API_KEY"

KEYRING_SERVICE = "prokube"
REFRESH_MARGIN_SECONDS = 60

# The workload token of a workspace pod, mounted by the PodDefault
# access-sandboxes (prokube/prokube-neo#100). Today a projected ServiceAccount
# token; any JWT file that something else keeps fresh works, such as a SPIRE
# JWT-SVID written by spiffe-helper.
TOKEN_FILE_ENV = "PROKUBE_SANDBOX_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "/var/run/secrets/prokube.ai/sandbox/token"


class LoginRequired(Exception):
    """There is no usable login for the platform."""


class LoginError(Exception):
    """The browser login did not complete."""


class LoginSession:
    """The stored login for one platform URL."""

    _store: Store

    def __init__(self, url: str) -> None:
        self.url = endpoints.normalize_url(url)
        loaded = _load_tokens(self.url)
        if loaded is None:
            raise LoginRequired(
                f"Not logged in to {self.url}. Run prokube.auth.login({self.url!r})."
            )
        self._tokens, self._store = loaded
        self._lock = threading.Lock()

    def token(self) -> str:
        """Return a valid ID token, refreshing it if it is about to expire."""
        with self._lock:
            if _expiring(self._tokens):
                # Another process may have refreshed (and rotated) it already.
                loaded = _load_tokens(self.url)
                if loaded is not None:
                    self._tokens, self._store = loaded
            if _expiring(self._tokens):
                self._tokens = _refresh(self.url, self._tokens)
                _save_tokens(self.url, self._tokens, self._store)
            return self._tokens["id_token"]


class TokenFile:
    """A token in a file that another process renews, such as the kubelet.

    The file is read again when the token read last is about to expire.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._expires_at - time.time() < REFRESH_MARGIN_SECONDS:
                self._token = self.path.read_text().strip()
                self._expires_at = _jwt_expiry(self._token)
            return self._token


def workload_token_file() -> Path | None:
    """The pod's workload token file, if there is one.

    ``$PROKUBE_SANDBOX_TOKEN_FILE``, else the path the PodDefault mounts.
    """
    path = Path(os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE)
    return path if path.is_file() else None


def default_token_provider(url: str | None) -> TokenProvider:
    """Where tokens come from when the caller passes none.

    The stored login for ``url``, else the pod's workload token file.
    """
    token_file = workload_token_file()
    if url:
        try:
            return LoginSession(url).token
        except LoginRequired:
            if token_file is None:
                raise
    if token_file is None:
        raise LoginRequired(
            "No login and no workload token. Run prokube.auth.login(url), or "
            f"mount a token at {DEFAULT_TOKEN_FILE} (set {TOKEN_FILE_ENV})."
        )
    return TokenFile(token_file).token


def credential(
    url: str | None,
    token_provider: TokenProvider | None = None,
    api_key: str | None = None,
) -> Credential:
    """The credential header for API calls.

    ``Authorization: Bearer`` with ``token_provider``'s tokens; else
    ``x-api-key`` with ``api_key`` or ``$PROKUBE_API_KEY``; else the bearer
    tokens of ``default_token_provider(url)``.
    """
    if token_provider is None:
        api_key = api_key or os.environ.get(API_KEY_ENV)
        if api_key:
            key = api_key
            return lambda: ("x-api-key", key)
        token_provider = default_token_provider(url)
    provider = token_provider
    return lambda: ("authorization", f"Bearer {provider()}")


def login(url: str, *, open_browser: bool = True, timeout: float = 300) -> LoginSession:
    """Log in through the browser and store the tokens for the platform url."""
    url = endpoints.normalize_url(url)
    dex = _discover(url)
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    server, redirect_uri = _start_callback_server()
    with server:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": endpoints.DEX_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "scope": endpoints.SCOPES,
                "state": state,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        auth_url = f"{dex['authorization_endpoint']}?{query}"
        print(f"Open this URL to log in:\n{auth_url}", file=sys.stderr)
        if open_browser:
            webbrowser.open(auth_url)
        params = _wait_for_callback(server, timeout)

    if not secrets.compare_digest(params.get("state", ""), state):
        raise LoginError("Login response does not match this login (state mismatch).")
    if "code" not in params:
        reason = params.get("error_description") or params.get("error", "no code")
        raise LoginError(f"Login failed: {reason}")
    response = httpx.post(
        dex["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": redirect_uri,
            "client_id": endpoints.DEX_CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    response.raise_for_status()
    # Dex revokes the previous refresh token, so no old copy may stay behind.
    logout(url)
    _save_tokens(url, _tokens_from_response(response.json()))
    return LoginSession(url)


def config_dir() -> Path:
    """The SDK's config directory: ``$PROKUBE_CONFIG_DIR`` or ``~/.config/prokube``."""
    default = Path.home() / ".config" / "prokube"
    return Path(os.environ.get("PROKUBE_CONFIG_DIR", default))


def load_config() -> dict[str, str]:
    """The platform URL and workspace that pk login saved, if any."""
    path = _config_file()
    return json.loads(path.read_text()) if path.exists() else {}


def save_config(url: str, workspace: str | None) -> None:
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = {"url": url} | ({"workspace": workspace} if workspace else {})
    path.write_text(json.dumps(config, indent=2) + "\n")


def _config_file() -> Path:
    return config_dir() / "config.json"


def logout(url: str) -> None:
    """Forget the stored tokens for the platform url."""
    url = endpoints.normalize_url(url)
    try:
        keyring.delete_password(KEYRING_SERVICE, url)
    except Exception:
        pass  # nothing stored there, or no keyring
    _token_file(url).unlink(missing_ok=True)


def _discover(url: str) -> dict[str, Any]:
    issuer = endpoints.dex_issuer(url)
    response = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=30)
    response.raise_for_status()
    metadata = response.json()
    if metadata.get("issuer") != issuer:
        raise LoginError(
            f"Dex reports issuer {metadata.get('issuer')!r}, expected {issuer!r}"
        )
    return metadata


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _refresh(url: str, tokens: dict[str, Any]) -> dict[str, Any]:
    expired = LoginRequired(
        f"The login for {url} expired. Run prokube.auth.login({url!r})."
    )
    if not tokens.get("refresh_token"):
        raise expired
    response = httpx.post(
        _discover(url)["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": endpoints.DEX_CLIENT_ID,
        },
        timeout=30,
    )
    if response.status_code in (400, 401):
        raise expired  # refresh token revoked, expired or already rotated away
    response.raise_for_status()
    return _tokens_from_response(response.json(), tokens["refresh_token"])


def _tokens_from_response(
    body: dict[str, Any], refresh_token: str | None = None
) -> dict[str, Any]:
    return {
        "id_token": body["id_token"],
        "refresh_token": body.get("refresh_token", refresh_token),
        "expires_at": _jwt_expiry(body["id_token"]),
    }


def _jwt_expiry(token: str) -> float:
    """Read ``exp`` from a JWT. Not verified: only used to schedule refreshes."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])


def _expiring(tokens: dict[str, Any]) -> bool:
    return tokens["expires_at"] - time.time() < REFRESH_MARGIN_SECONDS


# --- Local redirect target -------------------------------------------------


class _CallbackServer(HTTPServer):
    """Receives the redirect from Dex on localhost."""

    def __init__(self, port: int, path: str) -> None:
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.callback_path = path
        self.params: dict[str, str] | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    timeout = 10  # seconds a browser connection may stay silent

    def do_GET(self) -> None:
        server = cast(_CallbackServer, self.server)
        parsed = urlparse(self.path)
        if parsed.path != server.callback_path:
            self.send_error(404)  # e.g. /favicon.ico
            return
        server.params = {
            key: values[0] for key, values in parse_qs(parsed.query).items()
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Login finished. You can close this tab.")

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep the authorization code out of the terminal


def _start_callback_server() -> tuple[_CallbackServer, str]:
    for redirect_uri in endpoints.REDIRECT_URIS:
        parsed = urlparse(redirect_uri)
        try:
            return _CallbackServer(parsed.port or 80, parsed.path), redirect_uri
        except OSError:
            continue  # port in use, try the next one
    raise LoginError(f"None of the redirect ports is free: {endpoints.REDIRECT_URIS}")


def _wait_for_callback(server: _CallbackServer, timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while server.params is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LoginError("Timed out waiting for the browser login.")
        server.timeout = remaining
        server.handle_request()
    return server.params


# --- Token storage ---------------------------------------------------------
# Any keyring failure (no backend, locked, value too long) falls back to the file.
# Refreshed tokens go back to where they were read: Dex rotates the refresh token,
# so a process without a keyring would otherwise keep reading a spent one.

Store = Literal["keyring", "file"]


def _load_tokens(url: str) -> tuple[dict[str, Any], Store] | None:
    try:
        stored = keyring.get_password(KEYRING_SERVICE, url)
    except Exception:
        stored = None
    if stored:
        return json.loads(stored), "keyring"
    path = _token_file(url)
    if path.exists():
        return json.loads(path.read_text()), "file"
    return None


def _save_tokens(url: str, tokens: dict[str, Any], store: Store = "keyring") -> None:
    data = json.dumps(tokens)
    if store == "keyring":
        try:
            keyring.set_password(KEYRING_SERVICE, url, data)
            return
        except Exception:
            pass
    path = _token_file(url)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(data)
    os.chmod(path, 0o600)  # the mode above only applies to new files


def _token_file(url: str) -> Path:
    return config_dir() / "tokens" / f"{urlparse(url).netloc.replace(':', '_')}.json"
