"""Sandboxes on Agent Substrate: Actors named sbx-<name> in the workspace's Atespace."""

# The env client's results and errors, from agent-substrate/env (vendored).
from prokube.sandbox._ate_env import (
    Env,
    EnvError,
    FailedPreconditionError,
    InvalidArgumentError,
    NotFoundError,
    PermissionDeniedError,
    Process,
    RpcError,
    ShellResult,
)

# The Substrate API messages and enums the clients return (Actor states and the like).
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from prokube.sandbox.client import AsyncSandboxClient, SandboxClient, actor_name
from prokube.sandbox.network import add_rules, inject, passthrough
from prokube.sandbox.presets import Preset, PresetError, Presets

__all__ = [
    "AsyncSandboxClient",
    "Env",
    "EnvError",
    "FailedPreconditionError",
    "InvalidArgumentError",
    "NotFoundError",
    "PermissionDeniedError",
    "Preset",
    "PresetError",
    "Presets",
    "Process",
    "RpcError",
    "SandboxClient",
    "ShellResult",
    "actor_name",
    "add_rules",
    "ateapi",
    "inject",
    "passthrough",
]
