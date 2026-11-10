"""The pk command line. Needs the extra: pip install 'prokube-sdk[cli]'."""

import sys


def main() -> None:
    try:
        from prokube.cli.commands import cli
    except ImportError as error:
        sys.exit(f"pk needs the CLI extra: pip install 'prokube-sdk[cli]' ({error})")
    cli()
