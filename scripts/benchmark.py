#!/usr/bin/env python3
"""Time prokube sandboxes and E2B side by side with the same operations.

    uv run --with e2b==2.56.0 scripts/benchmark.py --e2b-key-file ~/.e2b-key

Each round runs, on both: create + first command, a command in the running
sandbox, 1 KiB and 1 MiB file write and read, suspend (E2B: pause), resume
until a command works (E2B: connect) and delete. A prokube sandbox is created
suspended and wakes on its first command. E2B pauses keep the memory only if
the prokube preset's snapshots do (fidelity MEMORY). Without --e2b-key-file,
only prokube is measured.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import math
import os
import socket
import statistics
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from prokube.sandbox import SandboxClient
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi

SIZES = {"1k": 1024, "1m": 1024 * 1024}
E2B_API_HOST = "api.e2b.app"
Round = dict[str, float]


@contextmanager
def timed(samples: Round, operation: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    except Exception as error:
        raise RuntimeError(f"{operation}: {type(error).__name__}: {error}") from error
    samples[operation] = time.perf_counter() - started


def check(stdout: str) -> None:
    if stdout.strip() != "ok":
        raise RuntimeError(f"unexpected output: {stdout!r}")


def prokube_round(client: SandboxClient, preset: str) -> Round:
    name = f"bench-{uuid.uuid4().hex[:8]}"
    samples: Round = {}
    created = None

    def run() -> None:
        result = client.run(name, "echo ok")
        if result.exit_code != 0:
            raise RuntimeError(f"exit code {result.exit_code}: {result.stderr!r}")
        check(result.stdout)

    try:
        with timed(samples, "create_first_command"):
            created = client.create(name, preset=preset)
            run()
        with timed(samples, "command"):
            run()
        for label, size in SIZES.items():
            data, path = os.urandom(size), f"/workspace/bench-{label}"
            with timed(samples, f"write_{label}"):
                client.write_file(name, path, data)
            with timed(samples, f"read_{label}"):
                if client.read_file(name, path) != data:
                    raise RuntimeError(f"{path} read back differently")
        with timed(samples, "suspend"):
            state = client.suspend(name).status.state
        if state != ateapi.ACTOR_STATE_SUSPENDED:
            raise RuntimeError(f"suspend returned {ateapi.ActorState.Name(state)}")
        with timed(samples, "resume_first_command"):
            client.resume(name)
            run()
    finally:
        if created is not None:
            with timed(samples, "delete"):
                client.delete(name)
    return samples


def e2b_round(api_key: str, template: str | None, pause_mode: str) -> Round:
    from e2b import Sandbox

    samples: Round = {}
    sandbox = None
    try:
        with timed(samples, "create_first_command"):
            sandbox = Sandbox.create(template, api_key=api_key)
            check(sandbox.commands.run("echo ok").stdout)
        with timed(samples, "command"):
            check(sandbox.commands.run("echo ok").stdout)
        for label, size in SIZES.items():
            data, path = os.urandom(size), f"/home/user/bench-{label}"
            with timed(samples, f"write_{label}"):
                sandbox.files.write(path, data)
            with timed(samples, f"read_{label}"):
                if bytes(sandbox.files.read(path, format="bytes")) != data:
                    raise RuntimeError(f"{path} read back differently")
        with timed(samples, "suspend"):
            sandbox.pause(mode=pause_mode)
        with timed(samples, "resume_first_command"):
            sandbox = Sandbox.connect(sandbox.sandbox_id, api_key=api_key)
            check(sandbox.commands.run("echo ok").stdout)
    finally:
        if sandbox is not None:
            with timed(samples, "delete"):
                sandbox.kill()
    return samples


def summary(rounds: list[Round]) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for operation in rounds[0] if rounds else []:
        ordered = sorted(r[operation] for r in rounds)
        result[operation] = {
            "count": len(ordered),
            "min_ms": round(ordered[0] * 1000, 1),
            "p50_ms": round(statistics.median(ordered) * 1000, 1),
            "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000, 1),
            "max_ms": round(ordered[-1] * 1000, 1),
        }
    return result


def print_table(result: dict[str, Any], arms: list[str]) -> None:
    print(f"\n{'p50 / p95 ms':<22}" + "".join(f"{arm:>22}" for arm in arms))
    for operation in result["prokube"]["timings"]:
        cells = []
        for arm in arms:
            t = result[arm]["timings"].get(operation)
            cells.append(f"{t['p50_ms']:.0f} / {t['p95_ms']:.0f}" if t else "-")
        print(f"{operation:<22}" + "".join(f"{cell:>22}" for cell in cells))


def tcp_connect_ms(host: str) -> float:
    """Median TCP connect time to host:443, for the client's network position."""
    samples = []
    for _ in range(5):
        started = time.perf_counter()
        socket.create_connection((host, 443), timeout=10).close()
        samples.append(time.perf_counter() - started)
    return round(statistics.median(samples) * 1000, 1)


def sdk_version(package: str) -> str | None:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None
    if package == "prokube-sdk":  # the version is not bumped per commit
        commit = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        version += f"+{commit}" if commit else ""
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Time prokube sandboxes and E2B side by side."
    )
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--preset", help="prokube preset (default: the platform's)")
    parser.add_argument("--e2b-key-file", type=Path, help="file with the E2B API key")
    parser.add_argument("--e2b-template", help="E2B template (default: E2B's)")
    parser.add_argument("--location", default="", help="where this client runs")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    return args


def main() -> None:
    args = parse_args()
    e2b_key = args.e2b_key_file.read_text().strip() if args.e2b_key_file else None
    started = datetime.datetime.now(datetime.timezone.utc)
    result: dict[str, Any] = {
        "started": started.isoformat(timespec="seconds"),
        "location": args.location,
        "rounds": args.rounds,
        "prokube": {"sdk": sdk_version("prokube-sdk")},
    }
    if e2b_key:
        result["e2b"] = {
            "sdk": sdk_version("e2b"),
            "template": args.e2b_template or "default",
            "tcp_connect_ms": tcp_connect_ms(E2B_API_HOST),
        }

    with SandboxClient() as client:
        preset = client.presets().get(args.preset)
        host = urlparse(client.url or "").hostname
        result["prokube"].update(
            url=client.url,
            workspace=client.workspace,
            preset=preset.name,
            size=preset.size,
            fidelity=preset.fidelity,
            tcp_connect_ms=tcp_connect_ms(host) if host else None,
        )
        arms = {"prokube": lambda: prokube_round(client, preset.name)}
        if e2b_key:
            pause_mode = "full" if preset.fidelity == "MEMORY" else "filesystem"
            result["e2b"]["pause_mode"] = pause_mode
            arms["e2b"] = lambda: e2b_round(e2b_key, args.e2b_template, pause_mode)
        rounds: dict[str, list[Round]] = {arm: [] for arm in arms}
        errors: dict[str, list[str]] = {arm: [] for arm in arms}
        for number in range(1, args.rounds + 1):
            print(f"round {number}/{args.rounds}", flush=True)
            for arm, run_round in arms.items():
                try:
                    rounds[arm].append(run_round())
                except Exception as error:  # keep going; failures are reported
                    errors[arm].append(str(error))
                    print(f"  {arm} failed: {errors[arm][-1]}", flush=True)

    for arm in arms:
        result[arm].update(errors=errors[arm], timings=summary(rounds[arm]))
    print_table(result, list(arms))
    if args.json_output:
        args.json_output.write_text(json.dumps(result, indent=2) + "\n")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
