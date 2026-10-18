"""Sandboxes on Agent Substrate: Actors named sbx-<name> in the workspace's Atespace."""

from prokube.sandbox.client import AsyncSandboxClient, SandboxClient, actor_name
from prokube.sandbox.settings import (
    Settings,
    SettingsError,
    Size,
    Sizes,
    SuggestedImage,
    SuggestedImages,
)

__all__ = [
    "AsyncSandboxClient",
    "SandboxClient",
    "Settings",
    "SettingsError",
    "Size",
    "Sizes",
    "SuggestedImage",
    "SuggestedImages",
    "actor_name",
]
