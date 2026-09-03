#!/usr/bin/env python3
"""Measure the public greenfield Sandbox path against a deployment."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import subprocess
import time
import uuid
from typing import Literal

from prokube import Sandbox, SandboxClient


def run_code_timed(sandbox: Sandbox, code: str, *, timeout: int = 300) -> float:
    started = time.perf_counter()
    result = sandbox.run_code(code, timeout=timeout)
    elapsed = time.perf_counter() - started
    if not result.success:
        raise RuntimeError(
            f"run_code failed: {result.error_name}: {result.error_value}"
        )
    return elapsed


def create_timed(
    client: SandboxClient, name: str, runtime: Literal["python", "python-microvm"]
) -> tuple[float, Sandbox]:
    started = time.perf_counter()
    sandbox = client.create(name=name, runtime=runtime)
    return time.perf_counter() - started, sandbox


def delete_timed(sandbox: Sandbox) -> float:
    started = time.perf_counter()
    sandbox.delete()
    return time.perf_counter() - started


def summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0] * 1000, 2),
        "p50_ms": round(statistics.median(ordered) * 1000, 2),
        "p95_ms": round(
            ordered[max(0, int(len(ordered) * 0.95 + 0.999) - 1)] * 1000, 2
        ),
        "max_ms": round(ordered[-1] * 1000, 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.environ.get("PROKUBE_API_URL"))
    parser.add_argument("--workspace", default=os.environ.get("PROKUBE_WORKSPACE"))
    parser.add_argument("--api-key", default=os.environ.get("PROKUBE_API_KEY"))
    parser.add_argument("--user-id", default=os.environ.get("PROKUBE_USER_ID"))
    parser.add_argument(
        "--runtime", choices=("python", "python-microvm"), default="python"
    )
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument(
        "--suspend-command",
        help="optional command with {name} placeholder; measures transparent resume-to-code",
    )
    parser.add_argument("--json-output")
    args = parser.parse_args()
    if not args.endpoint or not args.workspace:
        parser.error("--endpoint and --workspace are required")
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    return args


def main() -> None:
    args = parse_args()
    timings: dict[str, list[float]] = {
        "create_lazy": [],
        "first_code_cold": [],
        "active_code": [],
        "code_payload_upload_64k": [],
        "file_write_1k": [],
        "file_read_1k": [],
        "file_write_1m": [],
        "file_read_1m": [],
        "delete": [],
    }
    if args.suspend_command:
        timings["resume_to_code"] = []

    with SandboxClient(
        endpoint=args.endpoint,
        workspace=args.workspace,
        api_key=args.api_key,
        user_id=args.user_id,
        timeout=360,
    ) as client:
        for round_number in range(args.rounds):
            name = f"sdk-bench-{uuid.uuid4().hex[:10]}"
            create_seconds, sandbox = create_timed(client, name, args.runtime)
            timings["create_lazy"].append(create_seconds)
            try:
                timings["first_code_cold"].append(run_code_timed(sandbox, "value = 41"))
                timings["active_code"].append(
                    run_code_timed(sandbox, "value += 1; print(value)")
                )
                payload = "x" * (64 * 1024)
                timings["code_payload_upload_64k"].append(
                    run_code_timed(
                        sandbox, f"payload = {payload!r}; print(len(payload))"
                    )
                )
                started = time.perf_counter()
                sandbox.files.write("/workspace/benchmark.bin", b"x" * 1024)
                timings["file_write_1k"].append(time.perf_counter() - started)
                started = time.perf_counter()
                if len(sandbox.files.read("/workspace/benchmark.bin")) != 1024:
                    raise RuntimeError("1 KiB file download was truncated")
                timings["file_read_1k"].append(time.perf_counter() - started)
                started = time.perf_counter()
                sandbox.files.write("/workspace/benchmark-1m.bin", b"x" * 1048576)
                timings["file_write_1m"].append(time.perf_counter() - started)
                started = time.perf_counter()
                if len(sandbox.files.read("/workspace/benchmark-1m.bin")) != 1048576:
                    raise RuntimeError("1 MiB file download was truncated")
                timings["file_read_1m"].append(time.perf_counter() - started)
                if args.suspend_command:
                    command = shlex.split(args.suspend_command.format(name=name))
                    subprocess.run(
                        command,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    timings["resume_to_code"].append(
                        run_code_timed(sandbox, "value += 1; print(value)")
                    )
            finally:
                timings["delete"].append(delete_timed(sandbox))
            print(f"completed round {round_number + 1}/{args.rounds}", flush=True)

    result = {name: summary(samples) for name, samples in timings.items()}
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as output:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")


if __name__ == "__main__":
    main()
