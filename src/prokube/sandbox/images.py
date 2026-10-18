"""Pin a container image by digest, as ActorTemplates require (``name@sha256:...``).

A tag is resolved only where the registry hands out anonymous pull tokens
(Docker Hub, ghcr.io, quay.io and other public repositories). For anything
else, pass the digest.
"""

from __future__ import annotations

import re

import httpx

from prokube.sandbox.templates import TemplateError

PINNED = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
DOCKER_HUB = "registry-1.docker.io"
# Index types first, so multi-platform images resolve to their index.
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


async def resolve_image(image: str) -> str:
    """Return ``image@sha256:...``; an image that has a digest is returned as is."""
    if "@" in image:
        if not PINNED.fullmatch(image):
            raise TemplateError(f"{image}: expected NAME@sha256:<64 hex digits>.")
        return image
    registry, repository, tag = _parse(image)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
        digest = await _manifest_digest(http, registry, repository, tag)
    if digest is None:
        raise TemplateError(
            f"Cannot resolve {image} to a digest without registry credentials. "
            "Pass the image as NAME@sha256:<digest>."
        )
    return f"{image}@{digest}"


def _parse(image: str) -> tuple[str, str, str]:
    """Split ``[registry/]repository[:tag]``; Docker Hub and latest are the defaults."""
    name, tag = image, "latest"
    if ":" in image.rsplit("/", 1)[-1]:
        name, tag = image.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = DOCKER_HUB, name
    if registry in ("docker.io", "index.docker.io"):
        registry = DOCKER_HUB
    if registry == DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    return registry, repository, tag


async def _manifest_digest(
    http: httpx.AsyncClient, registry: str, repository: str, tag: str
) -> str | None:
    """The tag's manifest digest, using an anonymous token if the registry asks."""
    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": MANIFEST_TYPES}
    response = await http.head(url, headers=headers)
    if response.status_code == 401:
        token = await _anonymous_token(http, response.headers.get("www-authenticate"))
        if token is None:
            return None
        headers["Authorization"] = f"Bearer {token}"
        response = await http.head(url, headers=headers)
    digest = response.headers.get("docker-content-digest", "")
    if response.status_code != 200 or not digest.startswith("sha256:"):
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
