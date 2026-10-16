"""Small CLI using the same personal Sandbox client as Python applications."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .auth import Auth
from .client import SandboxClient
from .config import Credentials, Profiles, discover
from .errors import SandboxError


def parser():
    root = argparse.ArgumentParser(prog="pk")
    from prokube import __version__

    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--profile", help="Use a named platform profile")
    commands = root.add_subparsers(
        dest="command", required=True, metavar="{login,logout,auth,sandbox}"
    )
    login = commands.add_parser(
        "login", help="Sign in through your browser or import an existing UI JWT"
    )
    login.add_argument("url", nargs="?")
    login.add_argument("--credential-store", choices=["keyring", "file"])
    login.add_argument(
        "--token-file", type=Path, help="Import an existing UI JWT; no browser login"
    )
    login.add_argument(
        "--token-stdin", action="store_true", help="Read an existing UI JWT from stdin"
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the link without opening a browser",
    )
    commands.add_parser("logout", help="Remove the locally stored credential")
    auth = commands.add_parser("auth")
    sub = auth.add_subparsers(dest="action", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    sub.add_parser("logout")
    sandbox = commands.add_parser("sandbox")
    actions = sandbox.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create")
    create.add_argument("name")
    create.add_argument("--connect", action="store_true")
    create.add_argument("--json", action="store_true")
    actions.add_parser("list").add_argument("--json", action="store_true")
    for name in ("connect", "ssh-config"):
        cmd = actions.add_parser(name)
        cmd.add_argument("name")
        cmd.add_argument("--identity-file")
    execute = actions.add_parser("exec")
    execute.add_argument("name")
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    execute.add_argument("--timeout", type=int, default=3600)
    for name in ("suspend", "resume", "delete"):
        actions.add_parser(name).add_argument("name")
    copy = actions.add_parser("cp", help="Copy a file; remote paths use sandbox:/path")
    copy.add_argument("source")
    copy.add_argument("destination")
    hidden = commands.add_parser("_ssh-proxy")
    hidden.add_argument("name")
    hidden.add_argument("--subject", required=True)
    return root


def run(args) -> int:
    profiles = Profiles()
    if args.command == "login":
        if args.token_file and args.token_stdin:
            raise SandboxError("Choose one token input.")
        url = args.url
        name = args.profile
        try:
            existing = profiles.load(name)
        except SandboxError:
            existing = None
        if not url:
            if existing is not None:
                url, name = existing.platform_url, existing.name
            else:
                url = os.environ.get("PROKUBE_URL")
                if not url and sys.stdin.isatty():
                    url = input("Platform URL: ").strip()
        if not url:
            raise SandboxError("Run pk login <platform-url>.")
        storage = args.credential_store or (
            existing.credential_store if existing else "keyring"
        )
        profile = discover(url, name or "default", storage)
        store = Credentials(profile, profiles.root)
        if profile.credential_store == "keyring":
            store._keyring()  # Fail before browser login if secure storage is unavailable.
        auth = Auth(profile, store)
        if args.token_file or args.token_stdin:
            token = (
                args.token_file.read_text().strip()
                if args.token_file
                else sys.stdin.read().strip()
            )
            claims = auth.import_token(token)
        else:
            claims = auth.login(
                announce=lambda message: print(message, file=sys.stderr, flush=True),
                open_browser=not args.no_browser,
            )
        profiles.save(profile)
        display = claims.get("preferred_username", claims.get("name", claims["sub"]))
        print(f"Signed in as {display} ({profile.name}).")
        if profile.credential_store == "file":
            print(
                f"Credentials stored in an owner-readable file under {profiles.root} (explicit file storage).",
                file=sys.stderr,
            )
        return 0
    profile = profiles.load(args.profile)
    if args.command == "logout" or (args.command == "auth" and args.action == "logout"):
        Credentials(profile, profiles.root).clear()
        print("Signed out locally.")
        return 0
    if args.command == "auth":
        auth = Auth(profile)
        auth.token()
        saved = auth.credentials.read()
        if saved is None:
            raise SandboxError("Not signed in. Run pk login.")
        info = {
            "profile": profile.name,
            "platform": profile.platform_url,
            "user": saved["display_name"],
            "subject": saved["subject"],
            "expires_in_seconds": max(0, int(saved["expires"] - time.time())),
        }
        print(
            json.dumps(info)
            if args.json
            else f"Signed in as {info['user']} on {info['platform']} ({info['profile']})."
        )
        return 0
    if args.command == "_ssh-proxy":
        from .ssh import proxy

        proxy(profile, args.name, args.subject)
        return 0
    if args.action in ("connect", "ssh-config"):
        from .ssh import prepare, terminal

        if args.action == "connect":
            return terminal(
                profile, args.name, args.identity_file, replace_process=True
            )
        config = prepare(profile, args.name, args.identity_file)
        print(config.read_text(), end="")
        return 0
    with SandboxClient(profile) as client:
        if args.action == "create":
            sandbox = client.create(args.name)
            print(json.dumps({"id": sandbox.id}) if args.json else sandbox.id)
            if args.connect:
                from .ssh import terminal

                subject = client._async.auth._subject
                client.close()
                return terminal(
                    profile, sandbox.id, replace_process=True, expected_subject=subject
                )
        elif args.action == "list":
            rows = client.list()
            if args.json:
                print(json.dumps([asdict(row) for row in rows]))
            else:
                print("NAME\tSTATUS")
                for row in rows:
                    print(f"{row.id}\t{row.status}")
        elif args.action == "exec":
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not argv:
                raise SandboxError("Specify a command after --.")
            process = client.get(args.name).start_process(argv, timeout=args.timeout)
            try:
                for event in process.output(follow=True):
                    if event.stdout is not None:
                        sys.stdout.buffer.write(event.stdout)
                        sys.stdout.buffer.flush()
                    if event.stderr is not None:
                        sys.stderr.buffer.write(event.stderr)
                        sys.stderr.buffer.flush()
            except KeyboardInterrupt:
                process.kill()
                return 130
            return process.wait().exit_code
        elif args.action == "cp":
            remote_source = ":" in args.source
            remote_destination = ":" in args.destination
            if remote_source == remote_destination:
                raise SandboxError("Exactly one path must use sandbox:/path notation.")
            if remote_source:
                name, path = args.source.split(":", 1)
                with open(args.destination, "wb") as target:
                    for chunk in client.get(name).read_chunks(path):
                        target.write(chunk)
            else:
                name, path = args.destination.split(":", 1)
                with open(args.source, "rb") as source:
                    client.get(name).write_file(
                        path, iter(lambda: source.read(65536), b"")
                    )
        else:
            sandbox = client.get(args.name)
            getattr(sandbox, args.action)()
            print(f"{args.action}: {args.name}")
    return 0


def main():
    try:
        code = run(parser().parse_args())
    except KeyboardInterrupt:
        print("Canceled.", file=sys.stderr)
        code = 130
    except SandboxError as error:
        print(f"pk: {error}", file=sys.stderr)
        code = 1
    except (OSError, ValueError) as error:
        print(
            f"pk: local configuration or tool error ({type(error).__name__}).",
            file=sys.stderr,
        )
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
