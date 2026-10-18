"""The canonical image form of sandbox settings: ``<registry>/<repository>@sha256:<digest>``.

No tag; Docker Hub images are ``docker.io/library/<name>`` or
``docker.io/<org>/<name>``. A tag is resolved only where the registry hands
out anonymous pull tokens (Docker Hub, ghcr.io, quay.io and other public
repositories). For anything else, pass the digest.
"""

from __future__ import annotations

import re

import httpx

from prokube.sandbox.settings import SettingsError

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"
# Index types first, so multi-platform images resolve to their index.
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


async def canonical_image(image: str) -> str:
    """Return image in canonical form, resolving a tag to its digest."""
    name, _, digest = image.partition("@")
    registry, repository, tag = _parse(name)
    if digest:
        if not DIGEST.fullmatch(digest):
            raise SettingsError(f"{image}: expected NAME@sha256:<64 hex digits>.")
        return f"{registry}/{repository}@{digest}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
        digest = await _manifest_digest(http, registry, repository, tag)
    if digest is None:
        raise SettingsError(
            f"Cannot resolve {image} to a digest without registry credentials. "
            "Pass the image as NAME@sha256:<digest>."
        )
    return f"{registry}/{repository}@{digest}"


def _parse(name: str) -> tuple[str, str, str]:
    """Split ``[registry/]repository[:tag]``; Docker Hub and latest are the defaults."""
    tag = "latest"
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = DOCKER_HUB, name
    if registry in ("index.docker.io", DOCKER_HUB_API):
        registry = DOCKER_HUB
    if registry == DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    return registry, repository, tag


async def _manifest_digest(
    http: httpx.AsyncClient, registry: str, repository: str, tag: str
) -> str | None:
    """The tag's manifest digest, using an anonymous token if the registry asks."""
    host = DOCKER_HUB_API if registry == DOCKER_HUB else registry
    url = f"https://{host}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": MANIFEST_TYPES}
    response = await http.head(url, headers=headers)
    if response.status_code == 401:
        token = await _anonymous_token(http, response.headers.get("www-authenticate"))
        if token is None:
            return None
        headers["Authorization"] = f"Bearer {token}"
        response = await http.head(url, headers=headers)
    digest = response.headers.get("docker-content-digest", "")
    if response.status_code != 200 or not DIGEST.fullmatch(digest):
        return None
    return digest


async def _anonymous_token(
    http: httpx.AsyncClient, challenge: str | None
) -> str | None:
    """Fetch a pull token from the realm of a ``Bearer`` challenge, without credentials."""
    scheme, _, params = (challenge or "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
    realm = fields.pop("realm", None)
    if not realm:
        return None
    response = await http.get(realm, params=fields)
    if response.status_code != 200:
        return None
    body = response.json()
    return body.get("token") or body.get("access_token")
