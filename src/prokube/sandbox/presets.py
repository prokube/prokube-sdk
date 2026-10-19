"""The platform's sandbox presets (prokube/pkui#4678, phase 5).

A preset is a shared template in ``sandbox-templates`` with a golden snapshot,
so sandboxes from it start fast. The list and the default network rules of
new sandboxes come from ``GET /svc/sandbox/v1alpha/presets``
(prokube/prokube-neo#132) and are never hardcoded here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from google.protobuf import json_format

from prokube import endpoints
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi


class PresetError(ValueError):
    """The presets cannot be read, or a preset cannot be used as asked."""


@dataclass(frozen=True)
class Preset:
    """A preset; ``network`` is the EgressPolicy a new sandbox from it gets.

    That is the preset's own ``network`` if it has one, else the top-level one
    shared by all presets.
    """

    name: str
    template: str
    title: str
    description: str
    size: str
    # What a suspend keeps: "VOLUMES" (/workspace) or "MEMORY" (also memory and
    # processes); "" if the platform does not say. Resize stays within one.
    fidelity: str
    env_guest: bool
    network: ateapi.EgressPolicy

    @property
    def ref(self) -> ateapi.ObjectRef:
        return ateapi.ObjectRef(
            atespace=endpoints.TEMPLATE_ATESPACE, name=self.template
        )

    def __str__(self) -> str:
        return f"{self.name} ({self.title})" if self.title else self.name


@dataclass(frozen=True)
class Presets:
    """The platform's presets and the default one."""

    default: str
    presets: tuple[Preset, ...]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Presets:
        shared = data.get("network", {})
        return cls(
            default=data["default"],
            presets=tuple(
                Preset(
                    name=p["name"],
                    template=p["template"],
                    title=p.get("title", ""),
                    description=p.get("description", ""),
                    size=p["size"],
                    fidelity=p.get("fidelity", ""),
                    env_guest=p.get("envGuest", True),
                    network=json_format.ParseDict(
                        p.get("network", shared), ateapi.EgressPolicy()
                    ),
                )
                for p in data["presets"]
            ),
        )

    def get(self, name: str | None = None) -> Preset:
        """Return the preset called name, or the default one."""
        name = name or self.default
        for preset in self.presets:
            if preset.name == name:
                return preset
        choices = ", ".join(str(preset) for preset in self.presets)
        raise PresetError(f"Unknown preset {name!r}. Presets: {choices}")

    def of(self, ref: ateapi.ObjectRef) -> Preset | None:
        """The preset whose template ref is; None for any other template."""
        return next((p for p in self.presets if p.ref == ref), None)
