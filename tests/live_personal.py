"""Explicit real-platform acceptance; never collected as a mocked unit test.

Prepare the isolated identity with offsite-sandbox/service/tests/cli_fixture.py.
No password/token is printed. Real developer accounts and their Sandboxes are
not touched. Run with Playwright available and PK_E2E_IDENTITY_FILE pointing at
the protected fixture record; cleanup that fixture afterwards.
"""

import argparse
import fcntl
import json
import os
import pty
import queue
import secrets
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Optional live-test dependency, supplied with uv run --with playwright.
from playwright.sync_api import sync_playwright  # pyright: ignore[reportMissingImports]

from prokube import AsyncSandboxClient, SandboxClient
from prokube.personal.auth import Auth
from prokube.personal.config import Profiles


def run_cli(env, *args, **options):
    result = subprocess.run(
        [sys.executable, "-m", "prokube.personal.cli", "--profile", "e2e", *args],
        env=env,
        capture_output=True,
        timeout=90,
        **options,
    )
    if result.returncode:
        raise AssertionError(
            f"CLI {args[:2]} failed with code {result.returncode}: {result.stderr.decode()}"
        )
    return result.stdout.decode().strip()


def real_login(env, platform, identity):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "prokube.personal.cli",
            "--profile",
            "e2e",
            "login",
            platform,
            "--no-browser",
            "--credential-store=file",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = queue.Queue()

    def read():
        assert process.stderr is not None
        for line in process.stderr:
            lines.put(line.strip())

    threading.Thread(target=read, daemon=True).start()
    stage = "authorization link"
    try:
        deadline = time.monotonic() + 30
        url = ""
        while time.monotonic() < deadline:
            line = lines.get(timeout=5)
            if line.startswith("https://"):
                url = line
                break
        assert url
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.environ.get(
                    "CHROME_BIN",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                ),
                headless=True,
            )
            try:
                page = browser.new_page()
                stage = "Keycloak login page"
                page.goto(url)
                page.locator("#username").wait_for(timeout=30000)
                page.locator("#username").fill(identity["username"])
                page.locator("#password").fill(identity["password"])
                page.locator("#kc-login").click()
                stage = "Dex consent and loopback callback"
                deadline = time.monotonic() + 30
                while (
                    not page.url.startswith("http://localhost:")
                    and time.monotonic() < deadline
                ):
                    approve = page.get_by_role("button", name="Grant Access")
                    if approve.is_visible():
                        approve.click()
                    page.wait_for_timeout(250)
                assert page.url.startswith("http://localhost:")
                stage = "verified token and API identity"
                assert process.wait(timeout=30) == 0
                assert process.stdout is not None
                assert "Signed in as" in process.stdout.read()
            finally:
                browser.close()
    except Exception as error:
        # Browser errors can include form values/cookies. Never render them.
        raise AssertionError(
            f"Real login failed at {stage} ({type(error).__name__})"
        ) from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    print(
        "PASS: real browser code+PKCE login, fallback URL, identity verification and credential save",
        flush=True,
    )


def interactive_create(env, name):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 101, 0, 0))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "prokube.personal.cli",
            "--profile",
            "e2e",
            "sandbox",
            "create",
            name,
            "--connect",
        ],
        env=dict(env, TERM="xterm-256color"),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = b""

    def wait_for(value, timeout=60):
        nonlocal output
        deadline = time.monotonic() + timeout
        while value not in output and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    "interactive CLI exited before the terminal was ready"
                )
            if select.select([master], [], [], 0.25)[0]:
                output += os.read(master, 65536)
        if value not in output:
            raise AssertionError(
                "Terminal did not become ready: "
                + output.decode(errors="replace")[-2000:]
            )

    try:
        # tmux's default status-left width truncates the final ']' at this size.
        wait_for(b"[workspace")
        os.write(master, b"printf 'CLI-%s\\n' $((37*41))\n")
        wait_for(b"CLI-1517", 15)
        os.write(master, b"\x02d")
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)
    print(
        "PASS: pk sandbox create --connect, automatic SSH key/config, real tmux terminal and detach",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--platform", default="https://hen-public-troodon.prokube.cloud"
    )
    parser.add_argument("--identity-file", required=True)
    args = parser.parse_args()
    identity = json.loads(Path(args.identity_file).read_text())
    Path(".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".cache", prefix="personal-e2e-") as folder:
        root = Path(folder).resolve()
        env = dict(os.environ, PROKUBE_CONFIG_DIR=str(root / "config"), HOME=str(root))
        old = os.environ.get("PROKUBE_CONFIG_DIR")
        os.environ["PROKUBE_CONFIG_DIR"] = env["PROKUBE_CONFIG_DIR"]
        names = []
        try:
            real_login(env, args.platform, identity)
            profile = Profiles().load("e2e")
            auth = Auth(profile)
            saved = auth.credentials.read()
            assert saved is not None
            assert saved["subject"] == identity["dexSubject"] and saved.get(
                "refresh_token"
            )
            old_refresh = saved["refresh_token"]
            # Force proactive refresh without waiting for Dex's token lifetime.
            # This changes only the client cache timestamp; the real provider
            # performs refresh/rotation and the new JWT is signature-verified.
            with auth.credentials.lock:
                saved["expires"] = 1
                auth.credentials.save(saved)
            with ThreadPoolExecutor(max_workers=2) as pool:
                refreshed = list(pool.map(lambda _: Auth(profile).token(), range(2)))
            assert refreshed[0] == refreshed[1]
            rotated = Auth(profile).credentials.read()
            assert rotated is not None and rotated["refresh_token"] != old_refresh
            assert (
                json.loads(run_cli(env, "auth", "status", "--json"))["subject"]
                == identity["dexSubject"]
            )
            print(
                "PASS: real refresh rotation shared across concurrent client instances",
                flush=True,
            )
            name = "cli-check-" + secrets.token_hex(4)
            names.append(name)
            assert (
                json.loads(run_cli(env, "sandbox", "create", name, "--json"))["id"]
                == name
            )
            assert (
                run_cli(
                    env, "sandbox", "exec", name, "--", "sh", "-c", "printf cli-exec"
                )
                == "cli-exec"
            )
            source = root / "binary"
            source.write_bytes(bytes(range(256)) * 4096)
            run_cli(env, "sandbox", "cp", str(source), name + ":/workspace/binary")
            run_cli(
                env,
                "sandbox",
                "cp",
                name + ":/workspace/binary",
                str(root / "download"),
            )
            assert (root / "download").read_bytes() == source.read_bytes()
            with SandboxClient(profile) as client:
                sandbox = client.get(name)
                assert sandbox.run("printf sync-sdk").stdout == "sync-sdk"
                policy_and_secret_acceptance(sandbox, env, root, identity)
                process = sandbox.start_process(["cat"], stdin=True)
                process.write_input(b"sdk-input", close=True)
                assert (
                    b"".join(x.stdout or b"" for x in process.output(follow=True))
                    == b"sdk-input"
                )
                sandbox.suspend()
                sandbox.resume()
                assert sandbox.read_file("binary") == source.read_bytes()
            import asyncio

            async def asynchronous():
                async with AsyncSandboxClient(profile) as client:
                    sandbox = await client.get(name)
                    assert (await sandbox.run("printf async-sdk")).stdout == "async-sdk"

            asyncio.run(asynchronous())
            run_cli(env, "sandbox", "delete", name)
            names.remove(name)
            print(
                "PASS: CLI lifecycle/exec/copy, sync+async SDK, stdin/output and suspend/resume",
                flush=True,
            )
            name = "connect-check-" + secrets.token_hex(4)
            names.append(name)
            interactive_create(env, name)
            assert name in [
                x["id"] for x in json.loads(run_cli(env, "sandbox", "list", "--json"))
            ]
            run_cli(env, "sandbox", "delete", name)
            names.remove(name)
            run_cli(env, "logout")
            denied = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "prokube.personal.cli",
                    "--profile",
                    "e2e",
                    "sandbox",
                    "list",
                ],
                env=env,
                capture_output=True,
            )
            assert (
                denied.returncode != 0
                and b"pk login" in denied.stderr
                and b"Traceback" not in denied.stderr
            )
            print(
                "PASS: logout removes shared login; no Sandbox is implicitly deleted on disconnect/client close",
                flush=True,
            )
        finally:
            for name in names:
                try:
                    run_cli(env, "sandbox", "delete", name)
                except Exception:
                    print(
                        "Fixture operator must inspect leftover Sandbox:",
                        name,
                        file=sys.stderr,
                    )
            if old is None:
                os.environ.pop("PROKUBE_CONFIG_DIR", None)
            else:
                os.environ["PROKUBE_CONFIG_DIR"] = old


def policy_and_secret_acceptance(sandbox, env, root, identity):
    """Only the newly created fixture Sandbox is changed."""
    options = json.loads(run_cli(env, "sandbox", "egress", "options"))
    assert "example.com" in options["hosts"]
    assert sandbox.get_egress()["rules"] == []
    curl = "curl -fsS --connect-timeout 3 --max-time 8 https://example.com >/dev/null"
    assert sandbox.run(curl).exit_code != 0
    policy_file = root / "policy.json"
    policy_file.write_text(
        json.dumps({"rules": [{"host": "example.com", "protocol": "tls"}]})
    )
    applied = json.loads(
        run_cli(
            env, "sandbox", "egress", "apply", sandbox.id, "--file", str(policy_file)
        )
    )
    assert applied["revision"] and applied["rules"][0]["host"] == "example.com"
    applied = json.loads(
        run_cli(env, "sandbox", "egress", "allow", sandbox.id, "example.com")
    )
    assert len(applied["rules"]) == 1
    # Native policy propagation includes DNS and gateway caches.
    for _ in range(20):
        if sandbox.run(curl).exit_code == 0:
            break
        time.sleep(1)
    else:
        raise AssertionError("Allowed HTTPS destination did not become reachable")
    assert (
        sandbox.run(
            "curl -fsS --connect-timeout 3 --max-time 8 https://pypi.org >/dev/null"
        ).exit_code
        != 0
    )
    from prokube.personal.errors import SandboxError

    for rules, revision in [
        ([], "stale"),
        ([{"host": "api.ate-system.svc", "protocol": "tls"}], applied["revision"]),
    ]:
        try:
            sandbox.set_egress(rules, revision=revision)
        except SandboxError:
            pass
        else:
            raise AssertionError("Rejected policy input was accepted")
    run_cli(env, "sandbox", "egress", "clear", sandbox.id)
    for _ in range(20):
        if sandbox.run(curl).exit_code != 0:
            break
        time.sleep(1)
    else:
        raise AssertionError("New connections remained allowed after policy clear")
    print(
        "PASS: native EgressPolicy default deny, allow, unrelated-host deny, revision conflict, unapproved-host deny and clear",
        flush=True,
    )

    if identity.get("gatewayToken"):
        assert options["https_interception"] and any(
            c["name"] == "e2e-header" for c in options["credentials"]
        )
        sandbox.set_egress(
            [{"host": "httpbin.org", "protocol": "https", "credential": "e2e-header"}]
        )
        command = "curl -fsS --connect-timeout 3 --max-time 15 -H 'X-Prokube-Test: placeholder' https://httpbin.org/headers"
        for _ in range(20):
            result = sandbox.run(command)
            if result.exit_code == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError("Gateway-injected HTTPS request did not succeed")
        headers = json.loads(result.stdout)["headers"]
        assert headers.get("X-Prokube-Test") == "fixture " + identity["gatewayToken"]
        without = sandbox.run(
            "curl -fsS --connect-timeout 3 --max-time 15 https://httpbin.org/headers"
        )
        assert (
            without.exit_code == 0
            and "X-Prokube-Test" not in json.loads(without.stdout)["headers"]
        )
        sandbox.suspend()
        sandbox.resume()
        after = sandbox.run(command)
        assert (
            after.exit_code == 0
            and json.loads(after.stdout)["headers"].get("X-Prokube-Test")
            == "fixture " + identity["gatewayToken"]
        )
        sandbox.set_egress([])
        print(
            "PASS: real HTTPS gateway header replacement with isolated Kubernetes dummy Secret, projected CA verification, missing-header behavior and resume",
            flush=True,
        )

    value = secrets.token_urlsafe(32)
    secret_file = root / "secret-input"
    secret_file.touch(mode=0o600)
    secret_file.write_text(value)
    path = run_cli(
        env,
        "sandbox",
        "secret",
        "set",
        sandbox.id,
        "api-key",
        "--from-file",
        str(secret_file),
    )
    assert sandbox.read_file(path) == value.encode()
    stat = sandbox.run("stat -c '%a %U' " + shlex.quote(path))
    assert stat.stdout.strip() == "600 developer"
    assert (
        sandbox.run("stat -c '%a' /workspace/home/.prokube-secrets").stdout.strip()
        == "700"
    )
    rotated = secrets.token_urlsafe(32).encode()
    assert sandbox.write_secret("api-key", rotated) == path
    assert sandbox.read_file(path) == rotated
    # Explicitly verify/document the persistent, guest-visible nature of files.
    sandbox.suspend()
    sandbox.resume()
    assert sandbox.read_file(path) == rotated
    run_cli(env, "sandbox", "secret", "remove", sandbox.id, "api-key")
    assert sandbox.run("test ! -e " + shlex.quote(path)).exit_code == 0
    env_file = root / "private-env.json"
    env_file.touch(mode=0o600)
    env_file.write_text(json.dumps({"PK_TEST_SECRET": value}))
    # Report only a boolean; the injected value is never printed by the guest.
    assert (
        run_cli(
            env,
            "sandbox",
            "exec",
            "--env-file",
            str(env_file),
            sandbox.id,
            "--",
            "python3",
            "-c",
            "import os; print(bool(os.environ.get('PK_TEST_SECRET')))",
        )
        == "True"
    )
    assert sandbox.run('test -z "${PK_TEST_SECRET+x}"').exit_code == 0
    assert (
        sandbox.run(
            'test -n "$PK_TEST_SECRET"', env={"PK_TEST_SECRET": value}
        ).exit_code
        == 0
    )
    print(
        "PASS: CLI/SDK secret delivery, 0600/developer ownership, rotation, snapshot persistence, removal and process-scoped env injection",
        flush=True,
    )


if __name__ == "__main__":
    main()
