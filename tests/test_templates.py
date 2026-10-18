import pytest

from prokube.sandbox import templates
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.templates import Size, Sizes, TemplateError

# The shape of GET /svc/sandbox/v1alpha/sizes (prokube/prokube-neo#127). The
# values are test data; the SDK never hardcodes sizes.
SIZES_JSON = {
    "default": "small",
    "sizes": [
        {"name": "small", "cpu": "1", "memory": "2Gi", "pool": "default"},
        {"name": "medium", "cpu": "2", "memory": "4Gi", "pool": "default"},
        {"name": "gpu", "cpu": "2", "memory": "4Gi", "pool": "gpu"},
    ],
}
SIZES = Sizes.from_json(SIZES_JSON)
IMAGE = "registry.example.com/pk-sandbox-coding@sha256:" + "a" * 64
TRUST_BUNDLE = "/run/ate/egress-trust-bundle/trust-bundle.pem"


def build(**kwargs) -> ateapi.ActorTemplate:
    kwargs.setdefault("size", SIZES.get())
    return templates.build_template("team-a", "mine-v1", image=IMAGE, **kwargs)


def test_sizes_come_from_the_platform_json():
    assert SIZES.default == "small"
    assert SIZES.get() == Size("small", "1", "2Gi", "default")
    assert SIZES.get("gpu") == Size("gpu", "2", "4Gi", "gpu")


def test_unknown_size_lists_the_platform_sizes():
    with pytest.raises(TemplateError) as error:
        SIZES.get("huge")

    assert str(error.value) == (
        "Unknown size 'huge'. Sizes: small (1 CPU, 2Gi), medium (2 CPU, 4Gi), "
        "gpu (2 CPU, 4Gi)"
    )


def test_built_template_has_the_fixed_fields():
    template = build()

    assert template.metadata == ateapi.ResourceMetadata(
        atespace="team-a", name="mine-v1"
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
    assert container.image == IMAGE
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
    template = build(size=SIZES.get("gpu"))

    limits = [(limit.name, limit.quantity) for limit in template.resources.limits]
    assert limits == [("cpu", "2"), ("memory", "4Gi")]
    assert dict(template.worker_selector.match_labels) == {
        "prokube.ai/sandbox-pool": "gpu"
    }


def test_user_env_comes_first_and_the_egress_ca_variables_are_always_set():
    template = build(env={"MODE": "dev", "EMPTY": ""})

    env = [(e.name, e.value) for e in template.containers[0].env]
    assert env == [
        ("MODE", "dev"),
        ("EMPTY", ""),
        ("SSL_CERT_FILE", TRUST_BUNDLE),
        ("REQUESTS_CA_BUNDLE", TRUST_BUNDLE),
        ("CURL_CA_BUNDLE", TRUST_BUNDLE),
        ("GIT_SSL_CAINFO", TRUST_BUNDLE),
        ("PIP_CERT", TRUST_BUNDLE),
        ("NODE_EXTRA_CA_CERTS", TRUST_BUNDLE),
    ]


def test_user_env_cannot_replace_the_egress_ca_variables():
    with pytest.raises(TemplateError, match="SSL_CERT_FILE is set by the platform"):
        build(env={"SSL_CERT_FILE": "/etc/ssl/mine.pem"})


def test_env_guest_sets_the_wakeup_probe_marker():
    marked, unmarked = build(), build(env_guest=False)

    assert marked.containers[0].wakeup_probe == ateapi.WakeupProbe(
        http_get=ateapi.HttpGetAction(path="/readyz", port=80), timeout_seconds=30
    )
    assert not unmarked.containers[0].HasField("wakeup_probe")
    assert templates.has_env_guest(marked)
    assert not templates.has_env_guest(unmarked)


def test_another_probe_is_no_env_guest_marker():
    template = build()
    template.containers[0].wakeup_probe.http_get.path = "/"

    assert not templates.has_env_guest(template)


def test_template_size_matches_cpu_memory_and_pool():
    assert SIZES.of(build()) == SIZES.get("small")
    assert SIZES.of(build(size=SIZES.get("gpu"))) == SIZES.get("gpu")


def test_template_size_compares_quantities_by_value():
    same_value = build(size=Size("x", "1000m", "2048Mi", "default"))

    assert SIZES.of(same_value) == SIZES.get("small")


def test_template_size_is_custom_when_nothing_matches():
    other_pool = build(size=Size("x", "1", "2Gi", "other"))
    other_memory = build(size=Size("x", "1", "3Gi", "default"))
    no_pool = build()
    no_pool.ClearField("worker_selector")

    assert SIZES.of(other_pool) is None
    assert SIZES.of(other_memory) is None
    assert SIZES.of(no_pool) is None


def test_with_size_changes_only_resources_and_pool():
    template = build()
    template.status.golden_snapshot_status.error_message = "x"

    variant = templates.with_size(template, SIZES.get("gpu"))

    assert SIZES.of(variant) == SIZES.get("gpu")
    assert not variant.HasField("status")
    assert templates.same_except_size(variant, template)
    assert SIZES.of(template) == SIZES.get("small")  # the original is unchanged


def test_same_except_size_sees_other_differences():
    other_image = build()
    other_image.containers[0].image = IMAGE.replace("a" * 64, "b" * 64)

    assert not templates.same_except_size(build(), other_image)


def test_next_version_follows_the_highest_existing_one():
    existing = ["mine-v1", "mine-v3", "mine-v2-medium", "mine-v10x", "mine-v07"]
    existing += ["other-v7"]

    assert templates.next_version("mine", existing) == "mine-v4"
    assert templates.next_version("new", existing) == "new-v1"
    assert templates.next_version("mine.x", ["mineAx-v5"]) == "mine.x-v1"


def test_variant_name_appends_the_size():
    assert (
        templates.variant_name("coding-v2", SIZES.get("medium")) == "coding-v2-medium"
    )


def test_family_is_the_name_without_its_version():
    assert templates.family("coding-v2") == "coding"
    assert templates.family("my-tool-v12") == "my-tool"
    assert templates.family("coding-v2-medium") == "coding-v2-medium"
    assert templates.family("t125-gs") == "t125-gs"


def test_check_name_accepts_a_new_name():
    templates.check_name("my-tool", "my-tool-v1", ["coding-v2", "python-v1"], SIZES)


def test_check_name_rejects_an_approved_family():
    with pytest.raises(TemplateError) as error:
        templates.check_name("coding", "coding-v1", ["coding-v1", "coding-v2"], SIZES)

    assert str(error.value) == (
        "'coding' is the name of an approved template. Choose another name."
    )


@pytest.mark.parametrize("name", ["My-Tool", "tool_1", "-tool", "tool-", ""])
def test_check_name_needs_a_dns_label(name):
    with pytest.raises(TemplateError, match="must be a lowercase DNS label"):
        templates.check_name(name, f"{name}-v1", [], SIZES)


def test_check_name_leaves_room_for_every_size_variant():
    fits = "a" * (63 - len("-v1-medium"))  # medium is the longest size name
    too_long = fits + "a"

    templates.check_name(fits, f"{fits}-v1", [], SIZES)
    with pytest.raises(TemplateError) as error:
        templates.check_name(too_long, f"{too_long}-v1", [], SIZES)
    assert str(error.value) == (
        f"Template name {too_long!r} is too long: '{too_long}-v1' and its size "
        "variants must have at most 63 characters."
    )
