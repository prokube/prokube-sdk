"""Version compatibility checking for prokube SDK."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from prokube._version import __version__

if TYPE_CHECKING:
    from prokube.common.http import HttpClient

# Minimum required backend version for this SDK version
MIN_BACKEND_VERSION = "0.1.0"


def parse_version(version_string: str) -> tuple[int, int, int]:
    """Parse a version string into a normalized (major, minor, patch) tuple.

    Args:
        version_string: Version string like "1.2.3", "v0.1", "0.1.0-dev", or "1.2.3rc1".

    Returns:
        Tuple of (major, minor, patch) version numbers, e.g., (1, 2, 3).
        Missing components are defaulted to 0 (e.g., "0.1" -> (0, 1, 0)).
    """
    import re

    # Remove v prefix if present
    clean_version = version_string.lstrip("vV")
    # Remove any suffix like "-dev", "-alpha", "+build", etc.
    clean_version = clean_version.split("-")[0].split("+")[0]
    parts = clean_version.split(".")

    version_parts: list[int] = []
    for p in parts:
        # Extract leading numeric portion (handles "3rc1" -> 3)
        match = re.match(r"(\d+)", p)
        if match:
            version_parts.append(int(match.group(1)))

    # Normalize to exactly 3 components (major, minor, patch)
    while len(version_parts) < 3:
        version_parts.append(0)

    return (version_parts[0], version_parts[1], version_parts[2])


def check_backend_compatibility(client: HttpClient) -> None:
    """Check if the backend version is compatible with this SDK.

    Issues a warning if the backend version is older than the minimum
    required version. Does not raise an exception to allow graceful
    degradation.

    Args:
        client: HTTP client to use for version check.
    """
    try:
        response = client.get("/api/version")
        backend_version = response.get("version", "unknown")

        if backend_version == "unknown":
            return

        backend_tuple = parse_version(backend_version)
        min_tuple = parse_version(MIN_BACKEND_VERSION)

        if backend_tuple < min_tuple:
            warnings.warn(
                f"Backend version {backend_version} may be incompatible "
                f"with SDK version {__version__}. "
                f"Minimum required backend version: {MIN_BACKEND_VERSION}. "
                f"Some features may not work correctly.",
                UserWarning,
                stacklevel=3,
            )
    except Exception:
        # Don't fail if version check fails - the backend might not
        # have a version endpoint yet
        pass


def get_sdk_version() -> str:
    """Get the current SDK version.

    Returns:
        SDK version string.
    """
    return __version__
