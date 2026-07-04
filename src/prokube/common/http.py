"""Base HTTP client for prokube SDK."""

from __future__ import annotations

from typing import Any

import httpx

from prokube.common.auth import get_auth_headers
from prokube.common.config import Config
from prokube.common.exceptions import AuthenticationError, NotFoundError, ProKubeError


class HttpClient:
    """HTTP client for making requests to the prokube API."""

    def __init__(self, config: Config) -> None:
        """Initialize HTTP client.

        Args:
            config: SDK configuration.
        """
        self.config = config
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        """Get or create the httpx client."""
        if self._client is None:
            # For API key (external) access, the external routes (/sandbox/*,
            # /mcp/*) are top-level on the ingress gateway — NOT under the path
            # prefix (e.g., /pkui). So we strip the path and use only the origin.
            # For in-cluster access, use the configured Agent Gateway base URL;
            # sandbox paths are routed through /_platform/sandbox/...
            if self.config.use_api_key:
                base_url = self._get_origin(self.config.api_url)
            else:
                base_url = self.config.api_url
            if not base_url.endswith("/"):
                base_url += "/"
            self._client = httpx.Client(
                base_url=base_url,
                headers=get_auth_headers(self.config),
                timeout=self.config.timeout,
            )
        return self._client

    @staticmethod
    def _get_origin(url: str) -> str:
        """Extract origin (scheme + host + port) from a URL.

        Examples:
            https://example.com/pkui -> https://example.com
            https://example.com:8080/pkui -> https://example.com:8080
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"api_url must be an absolute URL including scheme, e.g. "
                f"'https://example.com'. Got: {url!r}"
            )
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"api_url must use http or https scheme. Got: {parsed.scheme!r}"
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    def _normalize_path(self, path: str) -> str:
        """Normalize path for httpx URL joining.

        Removes leading slash so httpx properly joins with base_url path.
        """
        return path.lstrip("/")

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make a GET request.

        Args:
            path: API path (will be joined with base_url).
            **kwargs: Additional arguments to pass to httpx.

        Returns:
            JSON response as dictionary.

        Raises:
            AuthenticationError: If authentication fails (401/403).
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        response = self.client.get(self._normalize_path(path), **kwargs)
        return self._handle_response(response)

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make a POST request.

        Args:
            path: API path (will be joined with base_url).
            **kwargs: Additional arguments to pass to httpx.

        Returns:
            JSON response as dictionary.

        Raises:
            AuthenticationError: If authentication fails (401/403).
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        response = self.client.post(self._normalize_path(path), **kwargs)
        return self._handle_response(response)

    def delete(self, path: str, **kwargs: Any) -> dict[str, Any] | None:
        """Make a DELETE request.

        Args:
            path: API path (will be joined with base_url).
            **kwargs: Additional arguments to pass to httpx.

        Returns:
            JSON response as dictionary, or None if no content.

        Raises:
            AuthenticationError: If authentication fails (401/403).
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        response = self.client.delete(self._normalize_path(path), **kwargs)
        self._check_status(response)
        # Handle empty response body (common for DELETE)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except Exception:
            # Response is not JSON, treat as success with no data
            return None

    def get_bytes(self, path: str, **kwargs: Any) -> bytes:
        """Make a GET request and return raw bytes.

        Args:
            path: API path (will be joined with base_url).
            **kwargs: Additional arguments to pass to httpx.

        Returns:
            Raw response bytes.

        Raises:
            AuthenticationError: If authentication fails (401/403).
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        response = self.client.get(self._normalize_path(path), **kwargs)
        self._check_status(response)
        return response.content

    def post_bytes(self, path: str, content: bytes, **kwargs: Any) -> dict[str, Any]:
        """Make a POST request with raw bytes.

        Args:
            path: API path (will be joined with base_url).
            content: Raw bytes to send.
            **kwargs: Additional arguments to pass to httpx.

        Returns:
            JSON response as dictionary.

        Raises:
            AuthenticationError: If authentication fails (401/403).
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        response = self.client.post(
            self._normalize_path(path), content=content, **kwargs
        )
        return self._handle_response(response)

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        """Handle HTTP response and return JSON.

        Args:
            response: HTTP response object.

        Returns:
            JSON response as dictionary.

        Raises:
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        self._check_status(response)
        return response.json()

    def _check_status(self, response: httpx.Response) -> None:
        """Check response status and raise appropriate errors.

        Args:
            response: HTTP response object.

        Raises:
            AuthenticationError: If authentication fails (401/403).
            NotFoundError: If resource is not found (404).
            ProKubeError: For other HTTP errors.
        """
        if response.status_code in (401, 403):
            try:
                error_detail = response.json().get("detail", response.text)
            except Exception:
                error_detail = response.text
            raise AuthenticationError(
                f"Authentication failed ({response.status_code}): {error_detail}",
                status_code=response.status_code,
            )
        if response.status_code == 404:
            raise NotFoundError(
                f"Resource not found: {response.url}",
                status_code=404,
            )
        if response.status_code >= 400:
            try:
                error_detail = response.json().get("detail", response.text)
            except Exception:
                error_detail = response.text
            raise ProKubeError(
                f"API request failed ({response.status_code}): {error_detail}",
                status_code=response.status_code,
            )
