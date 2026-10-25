"""The prokube wordmark above `pk --help` and `pk --version` in a terminal."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version

import click

WORDMARK: str = r"""
                 _          _
 _ __  _ __ ___ | | ___   _| |__   ___
| '_ \| '__/ _ \| |/ / | | | '_ \ / _ \
| |_) | | | (_) |   <| |_| | |_) |  __/
| .__/|_|  \___/|_|\_\\__,_|_.__/ \___|
|_|
""".strip("\n")

LAVENDER = (149, 141, 255)  # the prokube accent color, #958dff


def pk_version() -> str:
    return version("prokube-sdk")


def wanted() -> bool:
    """Only in a terminal, and not with PK_NO_BANNER set (pipes and scripts stay plain)."""
    return sys.stdout.isatty() and not os.environ.get("PK_NO_BANNER")


def render() -> str:
    """The wordmark with the version at the end of its last line."""
    lines = WORDMARK.splitlines()
    width = max(len(line) for line in lines)
    tag = f"v{pk_version()}"
    lines[-1] = lines[-1].ljust(width - len(tag)) + tag
    art = "\n".join(lines)
    if os.environ.get("NO_COLOR"):
        return art
    return click.style(art, fg=LAVENDER)
