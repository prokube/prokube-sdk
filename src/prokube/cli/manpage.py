"""The pk(1) man page, generated from the commands' help.

Regenerate after changing a command: python -m prokube.cli.manpage > man/pk.1
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

import click

from prokube._version import __version__
from prokube.cli.commands import cli

DESCRIPTION = """\
pk logs in to a prokube platform and creates and uses sandboxes in a workspace:
it runs commands in them, copies files, sets their network rules, connects over
SSH and serves them to MCP clients.

pk auth login remembers the platform and a default workspace. --url and
--workspace, or $PROKUBE_URL and $PROKUBE_WORKSPACE, override them per command.

In a workspace pod with sandbox access, such as a notebook, pk sandbox needs no
login and acts as the workspace. Only SSH (connect, forward, ssh-config,
create --connect) needs the platform URL."""

ENVIRONMENT = [
    ("PROKUBE_URL", "Platform URL, https://<domain>."),
    ("PROKUBE_WORKSPACE", "Workspace of the sandbox commands."),
    (
        "PROKUBE_API_KEY",
        "API key with the scope All sandboxes; used in place of a login.",
    ),
    (
        "PROKUBE_SANDBOX_TOKEN_FILE",
        "Workload token of a workspace pod "
        "(default: /var/run/secrets/prokube.ai/sandbox/token).",
    ),
    ("PROKUBE_CONFIG_DIR", "Configuration directory (default: ~/.config/prokube)."),
    (
        "PYTHON_KEYRING_BACKEND",
        "keyring.backends.fail.Keyring stores tokens in files instead of the "
        "OS keyring.",
    ),
    ("PK_NO_BANNER", "Leave out the prokube wordmark in a terminal."),
    ("NO_COLOR", "Show the wordmark without color."),
]

FILES = [
    ("~/.config/prokube/config.json", "Platform URL and default workspace."),
    (
        "~/.config/prokube/tokens/",
        "Login tokens, when no OS keyring is usable (mode 0600).",
    ),
    ("~/.config/prokube/ssh/id_ed25519", "pk's own SSH key, generated on first use."),
    ("~/.config/prokube/ssh/known_hosts", "Pinned host keys of the sandboxes."),
    (
        "~/.config/prokube/ssh/hosts/",
        "ssh config entries, pk.<workspace>.<name>.conf.",
    ),
]

EXAMPLES = """\
pk auth login https://my-platform.example.com --workspace my-workspace
pk sandbox create demo
pk sandbox exec demo -- python3 -c 'print(6 * 7)'
pk sandbox cp train.py demo:/workspace/
pk sandbox create agent --inject api.anthropic.com x-api-key anthropic/API_KEY
pk sandbox connect demo
pk sandbox delete demo
claude mcp add sandbox -- pk sandbox mcp"""


def render() -> str:
    """The man page in roff."""
    root = click.Context(cli, info_name="pk")
    lines = [
        f'.TH PK 1 "" "pk {__version__}" "prokube"',
        ".SH NAME",
        r"pk \- command line for the prokube platform",
        ".SH SYNOPSIS",
        r".B pk",
        r"[\fB\-\-version\fR] [\fB\-\-help\fR] \fICOMMAND\fR [\fIARGS\fR]...",
        ".SH DESCRIPTION",
        *_paragraphs(DESCRIPTION),
        ".PP",
        r"Every command takes \fB\-\-help\fR.",
        ".SH COMMANDS",
    ]
    for ctx in _commands(root):
        command = ctx.command
        usage = " ".join([ctx.command_path, *command.collect_usage_pieces(ctx)])
        # As click shows it: dedented (Python < 3.13 keeps docstring indents).
        help_text = inspect.cleandoc(command.help or "").partition("\f")[0]
        lines += [f".SS {_quote(usage)}", *_paragraphs(help_text)]
        for param in command.get_params(ctx):
            record = param.get_help_record(ctx)
            if not isinstance(param, click.Option) or not record:
                continue
            names, help_text = record
            if names == "--help":
                continue
            lines += [".TP", _option(names), _escape(help_text)]
    lines += [".SH ENVIRONMENT"]
    for name, text in ENVIRONMENT:
        lines += [".TP", rf"\fB{name}\fR", _escape(text)]
    lines += [".SH FILES"]
    for path, text in FILES:
        lines += [".TP", rf"\fI{_escape(path)}\fR", _escape(text)]
    lines += [".SH EXAMPLES", ".nf", ".RS 4", _escape(EXAMPLES), ".RE", ".fi"]
    lines += [
        ".SH SEE ALSO",
        r"\fBssh\fR(1)",
        ".PP",
        "https://github.com/prokube/prokube\\-sdk",
    ]
    # A heading starts a paragraph already.
    lines = [
        line
        for previous, line in zip(["", *lines[:-1]], lines, strict=True)
        if not (line == ".PP" and previous.startswith((".SH", ".SS")))
    ]
    return "\n".join(lines) + "\n"


def _commands(ctx: click.Context) -> Iterator[click.Context]:
    """The contexts of all commands below ctx's group, depth first."""
    group = ctx.command
    assert isinstance(group, click.Group)
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if command is None or command.hidden:
            continue
        child = click.Context(command, info_name=name, parent=ctx)
        if isinstance(command, click.Group):
            yield from _commands(child)
        else:
            yield child


def _paragraphs(text: str) -> list[str]:
    """Help text as roff paragraphs; a \\b paragraph keeps its lines."""
    lines: list[str] = []
    for paragraph in text.strip().split("\n\n"):
        if paragraph.startswith("\b\n"):
            body = paragraph.removeprefix("\b\n")
            lines += [".PP", ".nf", ".RS 4", _escape(body), ".RE", ".fi"]
        else:
            lines += [".PP", _escape(" ".join(paragraph.split()))]
    return lines


def _option(names: str) -> str:
    """'-p, --preset TEXT' as bold names and an italic metavar."""
    flags, _, metavar = names.partition(" ")
    if flags.endswith(","):  # '-p, --preset TEXT' splits after '-p,'
        rest, _, metavar = metavar.partition(" ")
        flags = f"{flags} {rest}"
    bold = ", ".join(rf"\fB{_escape(flag)}\fR" for flag in flags.split(", "))
    return bold + (rf" \fI{_escape(metavar)}\fR" if metavar else "")


def _quote(text: str) -> str:
    return '"' + _escape(text).replace('"', r"\(dq") + '"'


def _escape(text: str) -> str:
    text = text.replace("\\", r"\e").replace("-", r"\-")
    return "\n".join(
        rf"\&{line}" if line.startswith((".", "'")) else line
        for line in text.split("\n")
    )


if __name__ == "__main__":
    print(render(), end="")
