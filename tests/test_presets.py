import pytest

from prokube.sandbox import PresetError, Presets
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi

# The shape of GET /svc/sandbox/v1alpha/presets (prokube/prokube-neo#132). The
# values are test data; the SDK never hardcodes presets.
OPENCODE_RULE = {
    "tlsPassthrough": {
        "hostnames": ["opencode.ai", "models.opencode.ai"],
        "ports": {"numbers": [443]},
    }
}
PRESETS_JSON = {
    "default": "pk-default-small",
    "network": {"rules": [OPENCODE_RULE]},
    "presets": [
        *(
            {
                "name": f"pk-default-{size}",
                "template": f"pk-default-{size}-v1",
                "title": size.capitalize(),
                "description": "/workspace survives a suspend",
                "size": size,
                "envGuest": True,
            }
            for size in ("small", "medium", "large")
        ),
        *(
            {
                "name": f"pk-default-{size}-memory",
                "template": f"pk-default-{size}-memory-v1",
                "title": f"{size.capitalize()}, keeps memory",
                "description": "memory and running processes survive a suspend",
                "size": size,
                "envGuest": True,
            }
            for size in ("small", "medium", "large")
        ),
    ],
}
PRESETS = Presets.from_json(PRESETS_JSON)
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


def test_presets_come_from_the_platform_json():
    small = PRESETS.get()

    assert (small.name, small.title, small.size) == (
        "pk-default-small",
        "Small",
        "small",
    )
    assert small.ref == ateapi.ObjectRef(
        atespace="sandbox-templates", name="pk-default-small-v1"
    )
    assert small.network == OPENCODE_POLICY  # the shared network


def test_a_presets_own_network_replaces_the_shared_one():
    own = {**PRESETS_JSON["presets"][0], "network": {"rules": []}}

    presets = Presets.from_json({**PRESETS_JSON, "presets": [own]})

    assert presets.get().network == ateapi.EgressPolicy()


def test_without_any_network_a_preset_has_no_rules():
    presets = Presets.from_json(
        {"default": "pk-default-small", "presets": PRESETS_JSON["presets"]}
    )

    assert presets.get().network == ateapi.EgressPolicy()


def test_unknown_preset_lists_them():
    with pytest.raises(PresetError) as error:
        PRESETS.get("coding")

    assert str(error.value) == (
        "Unknown preset 'coding'. Presets: pk-default-small (Small), "
        "pk-default-medium (Medium), pk-default-large (Large), "
        "pk-default-small-memory (Small, keeps memory), "
        "pk-default-medium-memory (Medium, keeps memory), "
        "pk-default-large-memory (Large, keeps memory)"
    )


def test_a_preset_is_found_by_its_shared_template():
    shared = ateapi.ObjectRef(atespace="sandbox-templates", name="pk-default-large-v1")
    own = ateapi.ObjectRef(atespace="team-a", name="pk-default-large-v1")

    assert PRESETS.of(shared) == PRESETS.get("pk-default-large")
    assert PRESETS.of(own) is None
