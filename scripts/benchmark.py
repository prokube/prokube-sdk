#!/usr/bin/env python3
r"""Time prokube sandboxes and E2B side by side with the same operations.

    uv run --with e2b==2.56.0 --with e2b-code-interpreter==2.11.0 \
        scripts/benchmark.py --e2b-key-file ~/.e2b-key

Each round runs, on both: create + first command, a command in the running
sandbox, a first and a second Python snippet in one interpreter (prokube: a
python3 process fed on stdin, E2B: run_code), 1 KiB and 1 MiB file write and
read, suspend (E2B: pause), resume until a command works (E2B: connect) and
delete. A prokube sandbox is created suspended and wakes on its first command.
E2B pauses keep the memory only if the prokube preset's snapshots do (fidelity
MEMORY); then a third snippet checks that the interpreter kept its state.
Without --e2b-key-file, only prokube is measured.
"""

from __future__ import annotations

import argparse
import asyncio
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

from ate_env import Process

from prokube.sandbox import AsyncSandboxClient
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi

SIZES = {"1k": 1024, "1m": 1024 * 1024}
# Runs each line from stdin in one namespace; an exception ends the process.
PYTHON = ["python3", "-u", "-c", "import sys\nfor line in sys.stdin: exec(line)"]
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


def check(stdout: str, expected: str = "ok") -> None:
    if stdout.strip() != expected:
        raise RuntimeError(f"expected {expected!r}, got {stdout!r}")


class Python:
    """A python3 in a prokube sandbox that runs the snippets written to stdin."""

    def __init__(self, process: Process) -> None:
        self.process = process
        self.offset = 0

    async def run(self, code: str, expected: str) -> None:
        await self.process.write_input(code.encode() + b"\n")
        stdout = b""
        async for output in self.process.output(follow=True, stdout_offset=self.offset):
            if output.exit is not None:
                raise RuntimeError(f"python3 exited: {output.exit}")
            stdout += output.stdout or b""
            if stdout.endswith(b"\n"):
                break
        self.offset += len(stdout)
        check(stdout.decode(), expected)


async def prokube_round(client: AsyncSandboxClient, preset: str, memory: bool) -> Round:
    name = f"bench-{uuid.uuid4().hex[:8]}"
    samples: Round = {}
    created = None

    async def run() -> None:
        result = await client.run(name, "echo ok")
        if result.exit_code != 0:
            raise RuntimeError(f"exit code {result.exit_code}: {result.stderr!r}")
        check(result.stdout)

    try:
        with timed(samples, "create_first_command"):
            created = await client.create(name, preset=preset)
            await run()
        with timed(samples, "command"):
            await run()
        with timed(samples, "python_first"):
            python = Python(await client.env(name).start_process(PYTHON, stdin=True))
            await python.run("x = 41; print('ok')", "ok")
        with timed(samples, "python"):
            await python.run("x += 1; print(x)", "42")
        for label, size in SIZES.items():
            data, path = os.urandom(size), f"/workspace/bench-{label}"
            with timed(samples, f"write_{label}"):
                await client.write_file(name, path, data)
            with timed(samples, f"read_{label}"):
                if await client.read_file(name, path) != data:
                    raise RuntimeError(f"{path} read back differently")
        with timed(samples, "suspend"):
            state = (await client.suspend(name)).status.state
        if state != ateapi.ACTOR_STATE_SUSPENDED:
            raise RuntimeError(f"suspend returned {ateapi.ActorState.Name(state)}")
        with timed(samples, "resume_first_command"):
            await client.resume(name)
            await run()
        if memory:
            with timed(samples, "python_after_resume"):
                await python.run("x += 1; print(x)", "43")
    finally:
        if created is not None:
            with timed(samples, "delete"):
                await client.delete(name)
    return samples


def e2b_round(api_key: str, template: str, memory: bool) -> Round:
    from e2b_code_interpreter import Sandbox

    samples: Round = {}
    sandbox = None

    def python(code: str, expected: str) -> None:
        assert sandbox is not None
        execution = sandbox.run_code(code)
        if execution.error:
            raise RuntimeError(f"{execution.error.name}: {execution.error.value}")
        check("".join(execution.logs.stdout), expected)

    try:
        with timed(samples, "create_first_command"):
            sandbox = Sandbox.create(template, api_key=api_key)
            check(sandbox.commands.run("echo ok").stdout)
        with timed(samples, "command"):
            check(sandbox.commands.run("echo ok").stdout)
        with timed(samples, "python_first"):
            python("x = 41; print('ok')", "ok")
        with timed(samples, "python"):
            python("x += 1; print(x)", "42")
        for label, size in SIZES.items():
            data, path = os.urandom(size), f"/home/user/bench-{label}"
            with timed(samples, f"write_{label}"):
                sandbox.files.write(path, data)
            with timed(samples, f"read_{label}"):
                if bytes(sandbox.files.read(path, format="bytes")) != data:
                    raise RuntimeError(f"{path} read back differently")
        with timed(samples, "suspend"):
            sandbox.pause(mode="full" if memory else "filesystem")
        with timed(samples, "resume_first_command"):
            sandbox = Sandbox.connect(sandbox.sandbox_id, api_key=api_key)
            check(sandbox.commands.run("echo ok").stdout)
        if memory:
            with timed(samples, "python_after_resume"):
                python("x += 1; print(x)", "43")
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
    parser.add_argument("--e2b-template", help="default: code-interpreter-v1")
    parser.add_argument("--location", default="", help="where this client runs")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    return args


async def main() -> None:
    args = parse_args()
    e2b_key = args.e2b_key_file.read_text().strip() if args.e2b_key_file else None
    started = datetime.datetime.now(datetime.timezone.utc)
    result: dict[str, Any] = {
        "started": started.isoformat(timespec="seconds"),
        "location": args.location,
        "rounds": args.rounds,
        "prokube": {"sdk": sdk_version("prokube-sdk")},
    }
    e2b_template = ""
    if e2b_key:
        from e2b_code_interpreter import Sandbox

        e2b_template = args.e2b_template or Sandbox.default_template
        result["e2b"] = {
            "sdk": sdk_version("e2b"),
            "code_interpreter": sdk_version("e2b-code-interpreter"),
            "template": e2b_template,
            "tcp_connect_ms": tcp_connect_ms(E2B_API_HOST),
        }

    async with AsyncSandboxClient() as client:
        preset = (await client.presets()).get(args.preset)
        memory = preset.fidelity == "MEMORY"
        host = urlparse(client.url or "").hostname
        result["prokube"].update(
            url=client.url,
            workspace=client.workspace,
            preset=preset.name,
            size=preset.size,
            fidelity=preset.fidelity,
            tcp_connect_ms=tcp_connect_ms(host) if host else None,
        )
        arms = {"prokube": lambda: prokube_round(client, preset.name, memory)}
        if e2b_key:
            result["e2b"]["pause_mode"] = "full" if memory else "filesystem"
            arms["e2b"] = lambda: asyncio.to_thread(
                e2b_round, e2b_key, e2b_template, memory
            )
        rounds: dict[str, list[Round]] = {arm: [] for arm in arms}
        errors: dict[str, list[str]] = {arm: [] for arm in arms}
        for number in range(1, args.rounds + 1):
            print(f"round {number}/{args.rounds}", flush=True)
            for arm, run_round in arms.items():
                try:
                    rounds[arm].append(await run_round())
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
    asyncio.run(main())
