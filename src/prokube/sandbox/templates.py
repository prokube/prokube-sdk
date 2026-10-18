"""Presets, custom templates and sizes, as pkui builds them (prokube/pkui#4678).

A preset is a shared template in ``sandbox-templates`` with a golden snapshot,
so sandboxes from it start fast. A custom template is the ActorTemplate
``<workspace>/<name>-v<n>``; users choose its image, size and literal env,
everything else is fixed below. A size variant ``<workspace>/<template>-<size>``
is a template copied with another size's resources and worker pool. Presets
and sizes come from the platform (``GET /svc/sandbox/v1alpha/presets`` and
``.../sizes``) and are never hardcoded here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from google.protobuf import json_format

from prokube import endpoints
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi

ActorTemplate = ateapi.ActorTemplate

# WorkerPool label that a size's pool selects.
POOL_LABEL = "prokube.ai/sandbox-pool"
# Never user-settable: an unreachable location makes a template undeletable
# (prokube/prokube-neo#125).
STORAGE_LOCATION = "s3://ate-snapshots/sandboxes/"
CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "SYS_CHROOT")
TRUST_BUNDLE_DIR = "/run/ate/egress-trust-bundle"
# The egress gateway's MITM CA plus public roots, for every common TLS client.
CA_ENV = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "PIP_CERT",
    "NODE_EXTRA_CA_CERTS",
)
# The Actor's name and uid, per clone; the image keys its SSH host key on the
# uid (prokube/sandbox-images#20).
IDENTITY_DIR = "/run/ate/actor"
# The env guest of pk-sandbox-coding answers here once it serves; templates
# whose guest container has this wakeup probe support exec, files and SSH.
ENV_GUEST_PATH = "/readyz"
ENV_GUEST_PORT = 80
# Kubernetes name limit; a custom template's size variants must fit it, too.
MAX_NAME_LENGTH = 63
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
VERSIONED = re.compile(r"(?P<family>.+)-v(?P<version>[1-9][0-9]*)")
# Suffixes of Kubernetes quantities ("500m", "2Gi").
QUANTITY = re.compile(r"([0-9.]+(?:[eE][-+]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]?)")
MULTIPLIERS = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
MULTIPLIERS |= {suffix: 1000 ** (i + 1) for i, suffix in enumerate("kMGTPE")}
MULTIPLIERS |= {f"{suffix}i": 1024 ** (i + 1) for i, suffix in enumerate("KMGTPE")}


class TemplateError(ValueError):
    """A template, size or image cannot be used as given."""


@dataclass(frozen=True)
class Size:
    name: str
    cpu: str
    memory: str
    pool: str

    def __str__(self) -> str:
        return f"{self.name} ({self.cpu} CPU, {self.memory})"


@dataclass(frozen=True)
class Sizes:
    """The platform's sandbox sizes and the default one."""

    default: str
    sizes: tuple[Size, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Sizes:
        return cls(
            default=data["default"],
            sizes=tuple(
                Size(s["name"], s["cpu"], s["memory"], s["pool"]) for s in data["sizes"]
            ),
        )

    def get(self, name: str | None = None) -> Size:
        """Return the size called name, or the default one."""
        return _named(self.sizes, name or self.default, "size")

    def of(self, template: ActorTemplate) -> Size | None:
        """The size whose cpu, memory and pool equal the template's; None is custom.

        Quantities compare by value, so "1" equals "1000m".
        """
        limits = {limit.name: limit.quantity for limit in template.resources.limits}
        actual = (
            _quantity(limits.get("cpu")),
            _quantity(limits.get("memory")),
            template.worker_selector.match_labels.get(POOL_LABEL),
        )
        if None in actual:
            return None
        for size in self.sizes:
            if (_quantity(size.cpu), _quantity(size.memory), size.pool) == actual:
                return size
        return None


@dataclass(frozen=True)
class Preset:
    """A shared template the platform offers, with its default network rules."""

    name: str
    template: str  # in sandbox-templates
    title: str
    description: str
    size: str
    env_guest: bool
    network: ateapi.EgressPolicy | None

    def __str__(self) -> str:
        return f"{self.name} ({self.title})" if self.title else self.name


@dataclass(frozen=True)
class Presets:
    """The platform's presets and the default one."""

    default: str
    presets: tuple[Preset, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Presets:
        return cls(
            default=data["default"],
            presets=tuple(
                Preset(
                    name=p["name"],
                    template=p["template"],
                    title=p.get("title", ""),
                    description=p.get("description", ""),
                    size=p["size"],
                    env_guest=p.get("envGuest", True),
                    network=_egress_policy(p.get("network")),
                )
                for p in data["presets"]
            ),
        )

    def get(self, name: str | None = None) -> Preset:
        """Return the preset called name, or the default one."""
        return _named(self.presets, name or self.default, "preset")

    def of(self, ref: ateapi.ObjectRef) -> Preset | None:
        """The preset whose template ref is; None for a custom template."""
        for preset in self.presets:
            if (ref.atespace, ref.name) == (
                endpoints.TEMPLATE_ATESPACE,
                preset.template,
            ):
                return preset
        return None


def build_template(
    atespace: str,
    name: str,
    *,
    image: str,
    size: Size,
    env: Mapping[str, str] | None = None,
    env_guest: bool = True,
) -> ActorTemplate:
    """Return the ActorTemplate pkui and the CLI create; mirrors coding-v2.

    ``image`` must be pinned by digest. ``env_guest`` says the image is based
    on pk-sandbox-coding, which sets the wakeup probe on its env guest.
    """
    env = dict(env or {})
    for key in env:
        if key in CA_ENV:
            raise TemplateError(f"{key} is set by the platform (egress CA bundle).")
    trust_bundle = f"{TRUST_BUNDLE_DIR}/trust-bundle.pem"
    container = ateapi.Container(
        name="guest",
        image=image,
        env=[ateapi.EnvVar(name=key, value=value) for key, value in env.items()]
        + [ateapi.EnvVar(name=key, value=trust_bundle) for key in CA_ENV],
        security_context=ateapi.SecurityContext(
            capabilities=ateapi.Capabilities(add=CAPABILITIES)
        ),
        volume_mounts=[
            ateapi.VolumeMount(name="workspace", mount_path="/workspace"),
            ateapi.VolumeMount(name="egress-trust", mount_path=TRUST_BUNDLE_DIR),
            ateapi.VolumeMount(name="identity", mount_path=IDENTITY_DIR),
        ],
    )
    if env_guest:
        container.wakeup_probe.CopyFrom(
            ateapi.WakeupProbe(
                http_get=ateapi.HttpGetAction(path=ENV_GUEST_PATH, port=ENV_GUEST_PORT),
                timeout_seconds=30,
            )
        )
    trust_bundle_source = ateapi.TrustBundleDataSource(
        names=["egress-mitm.ate.dev", "system-roots.ate.dev"], path="trust-bundle.pem"
    )
    template = ateapi.ActorTemplate(
        metadata=ateapi.ResourceMetadata(atespace=atespace, name=name),
        containers=[container],
        volumes=[
            ateapi.Volume(
                name="workspace", durable_dir=ateapi.DurableDirVolumeSource()
            ),
            ateapi.Volume(
                name="egress-trust",
                system_info=ateapi.SystemInfoVolumeSource(
                    data_sources=[
                        ateapi.SystemInfoDataSource(trust_bundle=trust_bundle_source)
                    ]
                ),
            ),
            ateapi.Volume(
                name="identity",
                system_info=ateapi.SystemInfoVolumeSource(
                    data_sources=[
                        ateapi.SystemInfoDataSource(
                            actor_metadata=ateapi.ActorMetadataDataSource(
                                items=[
                                    ateapi.ActorMetadataItem(
                                        field=ateapi.ACTOR_METADATA_FIELD_NAME,
                                        path="name",
                                    ),
                                    ateapi.ActorMetadataItem(
                                        field=ateapi.ACTOR_METADATA_FIELD_UID,
                                        path="uid",
                                    ),
                                ]
                            )
                        )
                    ]
                ),
            ),
        ],
        snapshot_config=ateapi.SnapshotConfig(
            storage_location=STORAGE_LOCATION,
            preferred_fidelity=ateapi.SNAPSHOT_FIDELITY_VOLUMES,
            golden_snapshot_config=ateapi.GoldenSnapshotConfig(
                mode=ateapi.GOLDEN_SNAPSHOT_MODE_DISABLED
            ),
        ),
        sandbox_config=ateapi.SandboxConfig(
            sandbox_class="gvisor", config_name="gvisor-default"
        ),
    )
    return with_size(template, size)


def with_size(template: ActorTemplate, size: Size) -> ActorTemplate:
    """Return a copy of template with the size's resources and worker pool.

    The copy has no golden snapshot, even when template (a preset) has one.
    """
    sized = ateapi.ActorTemplate()
    sized.CopyFrom(template)
    sized.ClearField("status")
    sized.snapshot_config.golden_snapshot_config.CopyFrom(
        ateapi.GoldenSnapshotConfig(mode=ateapi.GOLDEN_SNAPSHOT_MODE_DISABLED)
    )
    sized.resources.CopyFrom(
        ateapi.Resources(
            limits=[
                ateapi.Limits(name="cpu", quantity=size.cpu),
                ateapi.Limits(name="memory", quantity=size.memory),
            ]
        )
    )
    sized.worker_selector.CopyFrom(
        ateapi.Selector(match_labels={POOL_LABEL: size.pool})
    )
    return sized


def next_version(name: str, existing: Iterable[str]) -> str:
    """``<name>-v<k+1>``, where k is the highest version among existing names."""
    versions = [
        int(m["version"])
        for n in existing
        if (m := VERSIONED.fullmatch(n)) and m["family"] == name
    ]
    return f"{name}-v{max(versions, default=0) + 1}"


def family(name: str) -> str:
    """``coding`` for ``coding-v2``; an unversioned name is its own family."""
    match = VERSIONED.fullmatch(name)
    return match["family"] if match else name


def check_name(
    name: str, versioned: str, shared: Iterable[str], presets: Presets, sizes: Sizes
) -> None:
    """Raise TemplateError unless ``versioned`` (``<name>-v<n>``) can be created.

    The rules are pkui's: a DNS label, neither a preset's name nor the family
    of a shared template, and short enough for every size variant
    ``<name>-v<n>-<size>``.
    """
    if not DNS_LABEL.fullmatch(name):
        raise TemplateError(
            f"Name {name!r} must be a lowercase DNS label (a-z, 0-9, '-')."
        )
    taken = {preset.name for preset in presets.presets}
    if name in taken | {family(template) for template in shared}:
        raise TemplateError(f"{name!r} is taken by a preset. Choose another name.")
    names = [versioned] + [variant_name(versioned, size) for size in sizes.sizes]
    if max(len(n) for n in names) > MAX_NAME_LENGTH:
        raise TemplateError(
            f"Name {name!r} is too long for a custom image: {versioned!r} and its "
            f"size variants must have at most {MAX_NAME_LENGTH} characters."
        )


def variant_name(template: str, size: Size) -> str:
    """Name of the size variant of a template, in the workspace's Atespace."""
    return f"{template}-{size.name}"


def same_except_size(a: ActorTemplate, b: ActorTemplate) -> bool:
    """Whether two templates differ only in name, size, golden snapshot and status."""

    def strip(template: ActorTemplate) -> ActorTemplate:
        copy = ateapi.ActorTemplate()
        copy.CopyFrom(template)
        for field in ("metadata", "status", "resources", "worker_selector"):
            copy.ClearField(field)
        copy.snapshot_config.ClearField("golden_snapshot_config")
        return copy

    return strip(a) == strip(b)


def has_env_guest(template: ActorTemplate) -> bool:
    """Whether the template's image is based on pk-sandbox-coding.

    Only then do exec, files, the terminal and SSH work in its sandboxes.
    """
    return any(
        container.name == "guest"
        and container.wakeup_probe.http_get.path == ENV_GUEST_PATH
        and container.wakeup_probe.http_get.port == ENV_GUEST_PORT
        for container in template.containers
    )


def _egress_policy(body: dict[str, Any] | None) -> ateapi.EgressPolicy | None:
    """An EgressPolicy from its proto3 JSON body, as the presets endpoint has it."""
    return None if body is None else json_format.ParseDict(body, ateapi.EgressPolicy())


def _named(items: tuple[Any, ...], name: str, kind: str) -> Any:
    for item in items:
        if item.name == name:
            return item
    choices = ", ".join(str(item) for item in items)
    raise TemplateError(f"Unknown {kind} {name!r}. {kind.capitalize()}s: {choices}")


def _quantity(value: str | None) -> Decimal | None:
    """The value of a Kubernetes quantity such as "500m" or "2Gi"; None if invalid."""
    match = QUANTITY.fullmatch(value or "")
    if not match:
        return None
    try:
        return Decimal(match[1]) * MULTIPLIERS[match[2]]
    except InvalidOperation:
        return None
