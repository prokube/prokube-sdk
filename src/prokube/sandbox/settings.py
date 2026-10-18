"""Sandbox settings: image, size, env and env guest (prokube/pkui#4678, phase 5).

A sandbox's settings live in an ActorTemplate in the workspace's Atespace,
named after its content (``Settings.template_name``), so pkui and the SDK
share one template for the same settings. Everything else in it is fixed
below. The sizes and suggested images come from the platform
(``GET /svc/sandbox/v1alpha/sizes`` and ``.../images``), never from this code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from google.protobuf import json_format

from prokube.sandbox._ateapi import ateapi_pb2 as ateapi

ActorTemplate = ateapi.ActorTemplate

# Size of a template whose resources match none of the platform's sizes.
CUSTOM = "custom"
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
# The env guest of pk-sandbox-coding answers here once it serves; templates
# whose guest container has this wakeup probe support exec, files and SSH.
ENV_GUEST_PATH = "/readyz"
ENV_GUEST_PORT = 80
# Suffixes of Kubernetes quantities ("500m", "2Gi").
QUANTITY = re.compile(r"([0-9.]+(?:[eE][-+]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]?)")
MULTIPLIERS = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
MULTIPLIERS |= {suffix: 1000 ** (i + 1) for i, suffix in enumerate("kMGTPE")}
MULTIPLIERS |= {f"{suffix}i": 1024 ** (i + 1) for i, suffix in enumerate("KMGTPE")}


class SettingsError(ValueError):
    """A size, image or env setting cannot be used as given."""


@dataclass(frozen=True)
class Settings:
    """What a sandbox's user chooses; ``size`` is a size name or ``custom``."""

    image: str
    size: str
    env: dict[str, str] = field(default_factory=dict)
    env_guest: bool = True

    def canonical_json(self) -> str:
        """The JSON the template name hashes; pkui computes the same bytes."""
        document = {
            "env": self.env,
            "envGuest": self.env_guest,
            "image": self.image,
            "size": self.size,
        }
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @property
    def template_name(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode()).hexdigest()
        return f"sbx-{digest[:12]}"

    def changed(
        self,
        *,
        image: str | None = None,
        size: str | None = None,
        env: Mapping[str, str | None] | None = None,
        env_guest: bool | None = None,
    ) -> Settings:
        """A copy with the given changes; an env value of None removes the variable."""
        merged = {**self.env, **(env or {})}
        return Settings(
            image=image or self.image,
            size=size or self.size,
            env={key: value for key, value in merged.items() if value is not None},
            env_guest=self.env_guest if env_guest is None else env_guest,
        )

    @classmethod
    def of(cls, template: ActorTemplate, sizes: Sizes) -> Settings:
        """Read the settings back from a sandbox's template."""
        container = template.containers[0]
        probe = container.wakeup_probe.http_get
        size = sizes.of(template)
        return cls(
            image=container.image,
            size=size.name if size else CUSTOM,
            env={e.name: e.value for e in container.env if e.name not in CA_ENV},
            env_guest=(probe.path, probe.port) == (ENV_GUEST_PATH, ENV_GUEST_PORT),
        )


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
class SuggestedImage:
    """An image the platform suggests, with the settings it prefills.

    ``network`` is the EgressPolicy a new sandbox from it gets.
    """

    name: str
    description: str
    image: str
    env_guest: bool
    env: dict[str, str]
    network: ateapi.EgressPolicy | None

    def __str__(self) -> str:
        return f"{self.name} ({self.description})" if self.description else self.name


@dataclass(frozen=True)
class SuggestedImages:
    """The platform's suggested images and the default one."""

    default: str
    images: tuple[SuggestedImage, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SuggestedImages:
        return cls(
            default=data["default"],
            images=tuple(
                SuggestedImage(
                    name=entry["name"],
                    description=entry.get("description", ""),
                    image=entry["image"],
                    env_guest=entry["envGuest"],
                    env=dict(entry.get("env", {})),
                    network=_egress_policy(entry.get("network")),
                )
                for entry in data["images"]
            ),
        )

    def get(self, name: str | None = None) -> SuggestedImage:
        """Return the suggested image called name, or the default one."""
        return _named(self.images, name or self.default, "suggested image")


def build_template(atespace: str, settings: Settings, size: Size) -> ActorTemplate:
    """Return the ActorTemplate pkui and the SDK create for settings at size.

    ``settings.image`` must be pinned by digest.
    """
    for key in settings.env:
        if key in CA_ENV:
            raise SettingsError(f"{key} is set by the platform (egress CA bundle).")
    trust_bundle = f"{TRUST_BUNDLE_DIR}/trust-bundle.pem"
    container = ateapi.Container(
        name="guest",
        image=settings.image,
        env=[ateapi.EnvVar(name=k, value=v) for k, v in sorted(settings.env.items())]
        + [ateapi.EnvVar(name=key, value=trust_bundle) for key in CA_ENV],
        security_context=ateapi.SecurityContext(
            capabilities=ateapi.Capabilities(add=CAPABILITIES)
        ),
        volume_mounts=[
            ateapi.VolumeMount(name="workspace", mount_path="/workspace"),
            ateapi.VolumeMount(name="egress-trust", mount_path=TRUST_BUNDLE_DIR),
        ],
    )
    if settings.env_guest:
        container.wakeup_probe.CopyFrom(
            ateapi.WakeupProbe(
                http_get=ateapi.HttpGetAction(path=ENV_GUEST_PATH, port=ENV_GUEST_PORT),
                timeout_seconds=30,
            )
        )
    trust_bundle_source = ateapi.TrustBundleDataSource(
        names=["egress-mitm.ate.dev", "system-roots.ate.dev"], path="trust-bundle.pem"
    )
    return ateapi.ActorTemplate(
        metadata=ateapi.ResourceMetadata(
            atespace=atespace, name=settings.template_name
        ),
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
        resources=ateapi.Resources(
            limits=[
                ateapi.Limits(name="cpu", quantity=size.cpu),
                ateapi.Limits(name="memory", quantity=size.memory),
            ]
        ),
        worker_selector=ateapi.Selector(match_labels={POOL_LABEL: size.pool}),
    )


def _egress_policy(body: dict[str, Any] | None) -> ateapi.EgressPolicy | None:
    """An EgressPolicy from its proto3 JSON body, as the images endpoint has it."""
    return None if body is None else json_format.ParseDict(body, ateapi.EgressPolicy())


def _named(items: tuple[Any, ...], name: str, kind: str) -> Any:
    for item in items:
        if item.name == name:
            return item
    choices = ", ".join(str(item) for item in items)
    raise SettingsError(f"Unknown {kind} {name!r}. {kind.capitalize()}s: {choices}")


def _quantity(value: str | None) -> Decimal | None:
    """The value of a Kubernetes quantity such as "500m" or "2Gi"; None if invalid."""
    match = QUANTITY.fullmatch(value or "")
    if not match:
        return None
    try:
        return Decimal(match[1]) * MULTIPLIERS[match[2]]
    except InvalidOperation:
        return None
