import pytest

from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.settings import (
    Settings,
    SettingsError,
    Size,
    Sizes,
    SuggestedImages,
    build_template,
)

# The shapes of GET /svc/sandbox/v1alpha/sizes and .../images
# (prokube/prokube-neo#127, #132). The values are test data; the SDK never
# hardcodes sizes or images.
SIZES_JSON = {
    "default": "small",
    "sizes": [
        {"name": "small", "cpu": "1", "memory": "2Gi", "pool": "default"},
        {"name": "medium", "cpu": "2", "memory": "4Gi", "pool": "default"},
        {"name": "gpu", "cpu": "2", "memory": "4Gi", "pool": "gpu"},
    ],
}
SIZES = Sizes.from_json(SIZES_JSON)
CODING = (
    "europe-west3-docker.pkg.dev/prokube-internal/prokube-customer/pk-sandbox-coding"
    "@sha256:d4feeac8a1521095906c0c655730a156edeb8f63af0911b18bd7c956a71fbb43"
)
NGINX = "docker.io/library/nginx@sha256:" + "0" * 64
OPENCODE_RULE = {
    "tlsPassthrough": {
        "hostnames": ["opencode.ai", "models.opencode.ai"],
        "ports": {"numbers": [443]},
    }
}
IMAGES_JSON = {
    "default": "coding",
    "images": [
        {
            "name": "coding",
            "description": "Coding sandbox with opencode, SSH and a terminal",
            "image": CODING,
            "envGuest": True,
            "env": {"OPENCODE_DISABLE_AUTOUPDATE": "1"},
            "network": {"rules": [OPENCODE_RULE]},
        },
        {
            "name": "python",
            "image": CODING,
            "envGuest": True,
            "network": {"rules": []},
        },
    ],
}
IMAGES = SuggestedImages.from_json(IMAGES_JSON)
OPENCODE_POLICY = ateapi.EgressPolicy(
    rules=[
        ateapi.EgressRule(
            tls_passthrough=ateapi.TlsPassthroughRule(
                hostnames=["opencode.ai", "models.opencode.ai"],
                ports=ateapi.Ports(numbers=[443]),
            )
        )
    ]
)
TRUST_BUNDLE = "/run/ate/egress-trust-bundle/trust-bundle.pem"

# Shared with pkui (prokube/pkui#4708): both must compute these names.
VECTORS = [
    (
        "sbx-22d9290a0255",
        Settings(CODING, "small"),
        '{"env":{},"envGuest":true,"image":"' + CODING + '","size":"small"}',
    ),
    (
        "sbx-79efbaf0453d",
        Settings(CODING, "medium"),
        '{"env":{},"envGuest":true,"image":"' + CODING + '","size":"medium"}',
    ),
    (
        "sbx-5c4abe566352",
        Settings(CODING, "small", {"OPENCODE_DISABLE_AUTOUPDATE": "1", "A": "x y"}),
        '{"env":{"A":"x y","OPENCODE_DISABLE_AUTOUPDATE":"1"},"envGuest":true,'
        '"image":"' + CODING + '","size":"small"}',
    ),
    (
        "sbx-2044fb864129",
        Settings(NGINX, "small", env_guest=False),
        '{"env":{},"envGuest":false,"image":"' + NGINX + '","size":"small"}',
    ),
    (
        "sbx-bceccb4bca35",
        Settings(NGINX, "small", {"GREETING": "grüß dich"}, env_guest=False),
        '{"env":{"GREETING":"grüß dich"},"envGuest":false,"image":"'
        + NGINX
        + '","size":"small"}',
    ),
]


def build(settings: Settings | None = None) -> ateapi.ActorTemplate:
    settings = settings or Settings(CODING, "small")
    return build_template("team-a", settings, SIZES.get(settings.size))


@pytest.mark.parametrize(("name", "settings", "canonical"), VECTORS)
def test_template_name_matches_the_shared_vectors(name, settings, canonical):
    assert settings.canonical_json() == canonical
    assert settings.template_name == name


def test_sizes_come_from_the_platform_json():
    assert SIZES.default == "small"
    assert SIZES.get() == Size("small", "1", "2Gi", "default")
    assert SIZES.get("gpu") == Size("gpu", "2", "4Gi", "gpu")


def test_unknown_size_lists_the_platform_sizes():
    with pytest.raises(SettingsError) as error:
        SIZES.get("huge")

    assert str(error.value) == (
        "Unknown size 'huge'. Sizes: small (1 CPU, 2Gi), medium (2 CPU, 4Gi), "
        "gpu (2 CPU, 4Gi)"
    )


def test_suggested_images_come_from_the_platform_json():
    coding = IMAGES.get()

    assert coding.name == "coding"
    assert coding.image == CODING
    assert coding.env_guest is True
    assert coding.env == {"OPENCODE_DISABLE_AUTOUPDATE": "1"}
    assert coding.network == OPENCODE_POLICY
    python = IMAGES.get("python")
    assert python.env == {}
    assert python.network == ateapi.EgressPolicy()


def test_unknown_suggested_image_lists_them():
    with pytest.raises(SettingsError) as error:
        IMAGES.get("rust")

    assert str(error.value) == (
        "Unknown suggested image 'rust'. Suggested images: coding (Coding sandbox "
        "with opencode, SSH and a terminal), python"
    )


def test_built_template_has_the_fixed_fields():
    template = build()

    assert template.metadata == ateapi.ResourceMetadata(
        atespace="team-a", name="sbx-22d9290a0255"
    )
    assert template.snapshot_config == ateapi.SnapshotConfig(
        storage_location="s3://ate-snapshots/sandboxes/",
        preferred_fidelity=ateapi.SNAPSHOT_FIDELITY_VOLUMES,
        golden_snapshot_config=ateapi.GoldenSnapshotConfig(
            mode=ateapi.GOLDEN_SNAPSHOT_MODE_DISABLED
        ),
    )
    assert template.sandbox_config == ateapi.SandboxConfig(
        sandbox_class="gvisor", config_name="gvisor-default"
    )
    [container] = template.containers
    assert container.name == "guest"
    assert container.image == CODING
    assert list(container.security_context.capabilities.add) == [
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETGID",
        "SETUID",
        "SYS_CHROOT",
    ]
    assert not container.command and not container.args
    assert [(m.name, m.mount_path) for m in container.volume_mounts] == [
        ("workspace", "/workspace"),
        ("egress-trust", "/run/ate/egress-trust-bundle"),
    ]
    workspace, egress_trust = template.volumes
    assert workspace == ateapi.Volume(
        name="workspace", durable_dir=ateapi.DurableDirVolumeSource()
    )
    [source] = egress_trust.system_info.data_sources
    assert list(source.trust_bundle.names) == [
        "egress-mitm.ate.dev",
        "system-roots.ate.dev",
    ]
    assert source.trust_bundle.path == "trust-bundle.pem"


def test_built_template_has_the_size_and_its_pool():
    template = build(Settings(CODING, "gpu"))

    limits = [(limit.name, limit.quantity) for limit in template.resources.limits]
    assert limits == [("cpu", "2"), ("memory", "4Gi")]
    assert dict(template.worker_selector.match_labels) == {
        "prokube.ai/sandbox-pool": "gpu"
    }


def test_user_env_comes_sorted_before_the_egress_ca_variables():
    template = build(Settings(CODING, "small", {"MODE": "dev", "EMPTY": ""}))

    env = [(e.name, e.value) for e in template.containers[0].env]
    assert env == [
        ("EMPTY", ""),
        ("MODE", "dev"),
        ("SSL_CERT_FILE", TRUST_BUNDLE),
        ("REQUESTS_CA_BUNDLE", TRUST_BUNDLE),
        ("CURL_CA_BUNDLE", TRUST_BUNDLE),
        ("GIT_SSL_CAINFO", TRUST_BUNDLE),
        ("PIP_CERT", TRUST_BUNDLE),
        ("NODE_EXTRA_CA_CERTS", TRUST_BUNDLE),
    ]


def test_user_env_cannot_replace_the_egress_ca_variables():
    with pytest.raises(SettingsError, match="SSL_CERT_FILE is set by the platform"):
        build(Settings(CODING, "small", {"SSL_CERT_FILE": "/etc/ssl/mine.pem"}))


def test_env_guest_sets_the_wakeup_probe_marker():
    marked = build()
    unmarked = build(Settings(NGINX, "small", env_guest=False))

    assert marked.containers[0].wakeup_probe == ateapi.WakeupProbe(
        http_get=ateapi.HttpGetAction(path="/readyz", port=80), timeout_seconds=30
    )
    assert not unmarked.containers[0].HasField("wakeup_probe")


@pytest.mark.parametrize("settings", [vector[1] for vector in VECTORS])
def test_settings_read_back_from_the_template(settings):
    assert Settings.of(build(settings), SIZES) == settings


def test_another_probe_is_no_env_guest():
    template = build()
    template.containers[0].wakeup_probe.http_get.path = "/"

    assert Settings.of(template, SIZES).env_guest is False


def test_template_size_compares_quantities_by_value():
    template = build()
    template.resources.limits[0].quantity = "1000m"
    template.resources.limits[1].quantity = "2048Mi"

    assert Settings.of(template, SIZES).size == "small"


def test_template_size_is_custom_when_nothing_matches():
    other_memory, other_pool, no_pool = build(), build(), build()
    other_memory.resources.limits[1].quantity = "3Gi"
    other_pool.worker_selector.match_labels["prokube.ai/sandbox-pool"] = "other"
    no_pool.ClearField("worker_selector")

    for template in (other_memory, other_pool, no_pool):
        assert Settings.of(template, SIZES).size == "custom"


def test_changed_merges_env_and_none_removes_a_variable():
    settings = Settings(CODING, "small", {"A": "1", "B": "2"})

    changed = settings.changed(size="medium", env={"B": None, "C": "3"})

    assert changed == Settings(CODING, "medium", {"A": "1", "C": "3"})
    assert settings.changed() == settings
    assert settings.changed(image=NGINX, env_guest=False) == Settings(
        NGINX, "small", {"A": "1", "B": "2"}, env_guest=False
    )
