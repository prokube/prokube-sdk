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
    "default": "coding",
    "presets": [
        {
            "name": "coding",
            "template": "coding-small-v1",
            "title": "Coding",
            "description": "opencode, SSH and a terminal",
            "size": "small",
            "envGuest": True,
            "network": {"rules": [OPENCODE_RULE]},
        },
        {
            "name": "coding-large",
            "template": "coding-large-v1",
            "title": "Coding, large",
            "size": "large",
            "envGuest": True,
            "network": {"rules": [OPENCODE_RULE]},
        },
        {
            "name": "python",
            "template": "python-small-v1",
            "title": "Python",
            "size": "small",
            "envGuest": True,
        },
        {
            "name": "sdk",
            "template": "sdk-small-v1",
            "title": "SDK",
            "description": "restores memory, for fast agent runs",
            "size": "small",
            "envGuest": True,
            "network": {"rules": []},
        },
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
    coding = PRESETS.get()

    assert (coding.name, coding.title, coding.size) == ("coding", "Coding", "small")
    assert coding.ref == ateapi.ObjectRef(
        atespace="sandbox-templates", name="coding-small-v1"
    )
    assert coding.network == OPENCODE_POLICY
    assert PRESETS.get("python").network == ateapi.EgressPolicy()


def test_unknown_preset_lists_them():
    with pytest.raises(PresetError) as error:
        PRESETS.get("rust")

    assert str(error.value) == (
        "Unknown preset 'rust'. Presets: coding (Coding), coding-large "
        "(Coding, large), python (Python), sdk (SDK)"
    )


def test_a_preset_is_found_by_its_shared_template():
    shared = ateapi.ObjectRef(atespace="sandbox-templates", name="python-small-v1")
    own = ateapi.ObjectRef(atespace="team-a", name="python-small-v1")

    assert PRESETS.of(shared) == PRESETS.get("python")
    assert PRESETS.of(own) is None
