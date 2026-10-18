"""Sandboxes on Agent Substrate: Actors named sbx-<name> in the workspace's Atespace."""

from prokube.sandbox.client import AsyncSandboxClient, SandboxClient, actor_name
from prokube.sandbox.templates import Size, Sizes, TemplateError

__all__ = [
    "AsyncSandboxClient",
    "SandboxClient",
    "Size",
    "Sizes",
    "TemplateError",
    "actor_name",
]
