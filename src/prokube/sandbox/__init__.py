"""Sandboxes on Agent Substrate: Actors named sbx-<name> in the workspace's Atespace."""

from prokube.sandbox.client import AsyncSandboxClient, SandboxClient, actor_name

__all__ = ["AsyncSandboxClient", "SandboxClient", "actor_name"]
