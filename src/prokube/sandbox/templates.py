"""Sandbox templates and sizes, as pkui builds them (prokube/pkui#4678, phase 4).

A custom template is the ActorTemplate ``<workspace>/<name>-v<n>``; users choose
its image, size and literal env, everything else is fixed below. A size
variant ``<workspace>/<template>-<size>`` is a template copied with another
size's resources and worker pool. The sizes come from the platform
(``GET /svc/sandbox/v1alpha/sizes``) and are never hardcoded here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

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
# The env guest of pk-sandbox-coding answers here once it serves; templates
# whose guest container has this wakeup probe support exec, files and SSH.
ENV_GUEST_PATH = "/readyz"
ENV_GUEST_PORT = 80


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
        name = name or self.default
        for size in self.sizes:
            if size.name == name:
                return size
        choices = ", ".join(str(size) for size in self.sizes)
        raise TemplateError(f"Unknown size {name!r}. Sizes: {choices}")

    def of(self, template: ActorTemplate) -> Size | None:
        """The size whose cpu, memory and pool equal the template's; None is custom."""
        limits = {limit.name: limit.quantity for limit in template.resources.limits}
        pool = template.worker_selector.match_labels.get(POOL_LABEL)
        for size in self.sizes:
            if (size.cpu, size.memory, size.pool) == (
                limits.get("cpu"),
                limits.get("memory"),
                pool,
            ):
                return size
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
    """Return a copy of template with the size's resources and worker pool."""
    sized = ateapi.ActorTemplate()
    sized.CopyFrom(template)
    sized.ClearField("status")
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
    pattern = re.compile(rf"{re.escape(name)}-v(\d+)")
    versions = [int(m.group(1)) for n in existing if (m := pattern.fullmatch(n))]
    return f"{name}-v{max(versions, default=0) + 1}"


def variant_name(template: str, size: Size) -> str:
    """Name of the size variant of a template, in the workspace's Atespace."""
    return f"{template}-{size.name}"


def same_except_size(a: ActorTemplate, b: ActorTemplate) -> bool:
    """Whether two templates differ only in name, size and status."""

    def strip(template: ActorTemplate) -> ActorTemplate:
        copy = ateapi.ActorTemplate()
        copy.CopyFrom(template)
        for field in ("metadata", "status", "resources", "worker_selector"):
            copy.ClearField(field)
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
