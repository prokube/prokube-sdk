import pytest

from prokube.sandbox.images import canonical_image
from prokube.sandbox.settings import SettingsError

DIGEST = "sha256:" + "c" * 64


@pytest.mark.parametrize(
    ("image", "canonical"),
    [
        ("registry.example.com/team/coding", "registry.example.com/team/coding"),
        ("busybox", "docker.io/library/busybox"),
        ("busybox:1.37.0", "docker.io/library/busybox"),
        ("docker.io/busybox", "docker.io/library/busybox"),
        ("index.docker.io/me/tool:1", "docker.io/me/tool"),
        ("me/tool", "docker.io/me/tool"),
        ("localhost:5000/app:dev", "localhost:5000/app"),
    ],
)
async def test_a_digest_is_kept_and_the_name_made_canonical(image, canonical):
    assert await canonical_image(f"{image}@{DIGEST}") == f"{canonical}@{DIGEST}"


@pytest.mark.parametrize(
    "image", ["busybox@sha256:abc", f"busybox@sha512:{'c' * 64}", f"a@b@{DIGEST}"]
)
async def test_a_malformed_digest_is_rejected(image):
    with pytest.raises(SettingsError, match="expected NAME@sha256"):
        await canonical_image(image)


async def test_a_docker_hub_tag_resolves_with_an_anonymous_token(httpx_mock):
    manifest = "https://registry-1.docker.io/v2/library/busybox/manifests/1.37.0"
    httpx_mock.add_response(
        method="HEAD",
        url=manifest,
        status_code=401,
        headers={
            "www-authenticate": 'Bearer realm="https://auth.docker.io/token",'
            'service="registry.docker.io",scope="repository:library/busybox:pull"'
        },
    )
    httpx_mock.add_response(
        url="https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository%3Alibrary%2Fbusybox%3Apull",
        json={"token": "anonymous"},
    )
    httpx_mock.add_response(
        method="HEAD",
        url=manifest,
        match_headers={"Authorization": "Bearer anonymous"},
        headers={"docker-content-digest": DIGEST},
    )

    assert await canonical_image("busybox:1.37.0") == (
        f"docker.io/library/busybox@{DIGEST}"
    )
    accept = httpx_mock.get_requests()[0].headers["accept"]
    assert accept.startswith("application/vnd.oci.image.index.v1+json")


async def test_registry_host_port_repository_and_default_tag(httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url="https://localhost:5000/v2/team/app/manifests/latest",
        headers={"docker-content-digest": DIGEST},
    )
    httpx_mock.add_response(
        method="HEAD",
        url="https://ghcr.io/v2/org/tool/manifests/v2",
        headers={"docker-content-digest": DIGEST},
    )

    assert await canonical_image("localhost:5000/team/app") == (
        f"localhost:5000/team/app@{DIGEST}"
    )
    assert await canonical_image("ghcr.io/org/tool:v2") == f"ghcr.io/org/tool@{DIGEST}"


async def test_a_private_image_asks_for_a_digest(httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url="https://private.example.com/v2/team/img/manifests/1.0",
        status_code=401,
        headers={
            "www-authenticate": 'Bearer realm="https://private.example.com'
            '/v2/token",service="private.example.com",scope="repository:team/img:pull"'
        },
    )
    httpx_mock.add_response(
        url="https://private.example.com/v2/token"
        "?service=private.example.com&scope=repository%3Ateam%2Fimg%3Apull",
        status_code=403,
    )

    with pytest.raises(SettingsError) as error:
        await canonical_image("private.example.com/team/img:1.0")

    assert str(error.value) == (
        "Cannot resolve private.example.com/team/img:1.0 to a digest without "
        "registry credentials. Pass the image as NAME@sha256:<digest>."
    )


async def test_a_missing_tag_asks_for_a_digest(httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url="https://quay.io/v2/org/app/manifests/nope",
        status_code=404,
    )

    with pytest.raises(SettingsError, match="NAME@sha256:<digest>"):
        await canonical_image("quay.io/org/app:nope")
