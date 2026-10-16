"""OIDC code+PKCE login, explicit UI-token import and serialized refresh."""

from __future__ import annotations

import hmac
import secrets
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from authlib.integrations.httpx_client import OAuth2Client

from .config import Credentials, Profile
from .errors import AuthenticationRequired, PermissionDenied, SandboxError


@contextmanager
def oauth_client(**options) -> Iterator[OAuth2Client]:
    client = OAuth2Client(**options)
    try:
        yield client
    finally:
        # Authlib's type surface omits its httpx.Client base, present at runtime.
        cast(httpx.Client, client).close()


class Auth:
    def __init__(self, profile: Profile, credentials: Credentials | None = None):
        self.profile = profile
        self.credentials = credentials or Credentials(profile)
        self._metadata = None
        self._keys = None
        self._subject = None

    def metadata(self) -> dict:
        if self._metadata is None:
            try:
                response = httpx.get(
                    self.profile.issuer.rstrip("/")
                    + "/.well-known/openid-configuration",
                    timeout=15,
                )
                response.raise_for_status()
                metadata = response.json()
                if metadata.get("issuer") != self.profile.issuer:
                    raise ValueError("issuer mismatch")
                for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    u = urlparse(metadata[field])
                    if u.scheme != "https" or not u.hostname or u.username:
                        raise ValueError("invalid endpoint")
                self._metadata = metadata
                self._keys = jwt.PyJWKClient(metadata["jwks_uri"], timeout=15)
            except (ValueError, httpx.HTTPError) as error:
                raise AuthenticationRequired(
                    "Cannot load the trusted OIDC provider configuration."
                ) from error
        return self._metadata

    def verify(
        self, token: str, *, login: bool = False, nonce: str | None = None
    ) -> dict:
        self.metadata()
        assert self._keys is not None
        try:
            key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256", "PS256"],
                audience=self.profile.client_id if login else self.profile.audiences,
                issuer=self.profile.issuer,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
            if login and (
                claims.get("azp", self.profile.client_id) != self.profile.client_id
                or (
                    isinstance(claims["aud"], list)
                    and len(claims["aud"]) > 1
                    and claims.get("azp") != self.profile.client_id
                )
            ):
                raise ValueError("authorized party mismatch")
            if nonce is not None and not hmac.compare_digest(
                str(claims.get("nonce", "")), nonce
            ):
                raise ValueError("nonce mismatch")
            return claims
        except (jwt.PyJWTError, ValueError) as error:
            raise AuthenticationRequired(
                "The identity token is invalid or expired. Run pk login."
            ) from error

    def check_access(self, token: str, claims: dict) -> None:
        try:
            response = httpx.get(
                self.profile.api_url + "/v1/me",
                headers={"Authorization": "Bearer " + token},
                timeout=15,
            )
        except httpx.HTTPError as error:
            raise AuthenticationRequired(
                "Cannot verify this login with the Sandbox API."
            ) from error
        if response.status_code != 200:
            raise PermissionDenied(
                "Signed in, but the Sandbox API has not granted this identity access. Ask the platform operator."
            )
        if response.json().get("subject") != claims["sub"]:
            raise AuthenticationRequired("Platform returned a different user identity.")

    def import_token(self, token: str) -> dict:
        claims = self.verify(token)
        self.check_access(token, claims)
        with self.credentials.lock:
            self.credentials.save(
                {
                    "id_token": token,
                    "subject": claims["sub"],
                    "expires": claims["exp"],
                    "display_name": claims.get(
                        "preferred_username", claims.get("name", claims["sub"])
                    ),
                }
            )
        return claims

    def token(self) -> str:
        with self.credentials.lock:
            saved = self.credentials.read()
            if not saved:
                raise AuthenticationRequired("Not signed in. Run pk login.")
            if self._subject is not None and self._subject != saved["subject"]:
                raise AuthenticationRequired(
                    "Active identity changed. Create a new SandboxClient."
                )
            self._subject = saved["subject"]
            remaining = saved["expires"] - time.time()
            if remaining > 60 or (remaining > 0 and not saved.get("refresh_token")):
                return saved["id_token"]
            if not saved.get("refresh_token"):
                raise AuthenticationRequired(
                    "Session expired. Run pk login or import a fresh UI token."
                )
            try:
                with oauth_client(
                    client_id=self.profile.client_id,
                    token_endpoint_auth_method="none",
                    timeout=15,
                ) as client:
                    response = client.refresh_token(
                        self.metadata()["token_endpoint"],
                        refresh_token=saved["refresh_token"],
                    )
                claims = self.verify(response["id_token"], login=True)
                if claims["sub"] != saved["subject"]:
                    raise ValueError("identity changed during refresh")
                if claims.get("nonce") and claims["nonce"] != saved.get("nonce"):
                    raise ValueError("nonce changed during refresh")
            except Exception as error:
                raise AuthenticationRequired(
                    "Session could not be refreshed. Run pk login again."
                ) from error
            saved.update(
                id_token=response["id_token"],
                expires=claims["exp"],
                refresh_token=response.get("refresh_token", saved["refresh_token"]),
            )
            self.credentials.save(saved)
            return saved["id_token"]

    def login(self, *, announce=print, open_browser=True, timeout=180) -> dict:
        metadata = self.metadata()
        if (
            not self.profile.client_id
            or self.profile.client_id not in self.profile.audiences
        ):
            raise AuthenticationRequired(
                "The platform has not enabled native CLI login."
            )
        state, nonce, verifier = (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(64),
        )
        received = {}
        expected_host = ""
        expected_path = ""

        class Callback(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, format: str, *args: object) -> None:
                pass  # Authorization codes/state must not enter access logs.

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                valid = (
                    self.headers.get("Host") == expected_host
                    and parsed.path == expected_path
                )
                valid = (
                    valid
                    and len(params.get("state", [])) == 1
                    and hmac.compare_digest(params["state"][0].encode(), state.encode())
                )
                if not valid:
                    self.send_error(400, "Invalid sign-in response")
                    return
                if len(params.get("code", [])) == 1:
                    received["code"] = params["code"][0]
                elif "error" in params:
                    received["error"] = True
                else:
                    self.send_error(400, "Missing authorization code")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    b"<h1>Return to your terminal</h1>You can close this tab."
                )

        server = None
        redirect = ""
        for redirect in self.profile.redirect_uris:
            parsed = urlparse(redirect)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in ("localhost", "127.0.0.1")
                or not parsed.port
            ):
                continue
            try:
                server = HTTPServer(("127.0.0.1", parsed.port), Callback)
                expected_host, expected_path = parsed.netloc, parsed.path or "/"
                break
            except OSError:
                continue
        if server is None:
            raise SandboxError(
                "Cannot bind a registered loopback login port. Close applications using ports 8000/18000 and retry."
            )
        server.timeout = 0.5
        try:
            with oauth_client(
                client_id=self.profile.client_id,
                redirect_uri=redirect,
                scope="openid profile email offline_access",
                token_endpoint_auth_method="none",
                code_challenge_method="S256",
                timeout=15,
            ) as client:
                url, _ = client.create_authorization_url(
                    metadata["authorization_endpoint"],
                    state=state,
                    nonce=nonce,
                    code_verifier=verifier,
                )
                announce(
                    "Opening your browser to sign in…\nIf the browser does not open, visit:\n"
                    + url
                )
                if open_browser:
                    try:
                        webbrowser.open(url)
                    except (webbrowser.Error, OSError):
                        pass  # The displayed URL remains usable on headless systems.
                deadline = time.monotonic() + timeout
                while not received and time.monotonic() < deadline:
                    server.handle_request()
                if not received or received.get("error"):
                    raise AuthenticationRequired(
                        "Sign-in was canceled or timed out. Run pk login again."
                    )
                try:
                    tokens = client.fetch_token(
                        metadata["token_endpoint"],
                        code=received["code"],
                        code_verifier=verifier,
                    )
                except Exception as error:
                    raise AuthenticationRequired(
                        "Authorization code could not be exchanged. Retry pk login."
                    ) from error
            claims = self.verify(tokens["id_token"], login=True, nonce=nonce)
            self.check_access(tokens["id_token"], claims)
            with self.credentials.lock:
                self.credentials.save(
                    {
                        "id_token": tokens["id_token"],
                        "refresh_token": tokens.get("refresh_token"),
                        "subject": claims["sub"],
                        "expires": claims["exp"],
                        "nonce": nonce,
                        "display_name": claims.get(
                            "preferred_username", claims.get("name", claims["sub"])
                        ),
                    }
                )
            return claims
        finally:
            server.server_close()
