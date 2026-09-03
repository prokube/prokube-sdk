#!/usr/bin/env python3
"""Measure durable suspend and resume as workspace file volume grows."""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import statistics
import subprocess
import time
import uuid

from prokube import SandboxClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.environ.get("PROKUBE_API_URL"))
    parser.add_argument("--workspace", default=os.environ.get("PROKUBE_WORKSPACE"))
    parser.add_argument("--api-key", default=os.environ.get("PROKUBE_API_KEY"))
    parser.add_argument("--user-id", default=os.environ.get("PROKUBE_USER_ID"))
    parser.add_argument("--sizes-mib", default="0,1,8,32")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--suspend-command",
        required=True,
        help="blocking durable suspend command with a {name} placeholder",
    )
    args = parser.parse_args()
    if not args.endpoint or not args.workspace:
        parser.error("--endpoint and --workspace are required")
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    try:
        args.sizes_mib = [int(value) for value in args.sizes_mib.split(",")]
    except ValueError:
        parser.error("--sizes-mib must be a comma-separated integer list")
    if not args.sizes_mib or any(value < 0 for value in args.sizes_mib):
        parser.error("--sizes-mib values must be non-negative")
    return args


def summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "minSeconds": round(ordered[0], 3),
        "p50Seconds": round(statistics.median(ordered), 3),
        "maxSeconds": round(ordered[-1], 3),
    }


def main() -> None:
    args = parse_args()
    results: dict[int, dict[str, list[float]]] = {
        size: {"upload": [], "suspend": [], "resumeToCode": []}
        for size in args.sizes_mib
    }
    with SandboxClient(
        endpoint=args.endpoint,
        workspace=args.workspace,
        api_key=args.api_key,
        user_id=None if args.api_key else args.user_id,
        timeout=360,
    ) as client:
        for size_mib in args.sizes_mib:
            for round_number in range(args.rounds):
                name = f"sdk-resume-{size_mib}m-{uuid.uuid4().hex[:8]}"
                sandbox = client.create(name=name)
                try:
                    active = sandbox.run_code("baseline = 1; print(baseline)")
                    if not active.success:
                        raise RuntimeError(active.error_value)

                    upload_started = time.perf_counter()
                    remaining = size_mib * 1024 * 1024
                    rng = random.Random((size_mib << 16) + round_number)
                    part = 0
                    while remaining:
                        chunk_size = min(remaining, 8 * 1024 * 1024)
                        sandbox.files.write(
                            f"/workspace/resume-data/part-{part:03d}.bin",
                            rng.randbytes(chunk_size),
                        )
                        remaining -= chunk_size
                        part += 1
                    results[size_mib]["upload"].append(
                        time.perf_counter() - upload_started
                    )

                    command = shlex.split(args.suspend_command.format(name=name))
                    suspend_started = time.perf_counter()
                    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
                    results[size_mib]["suspend"].append(
                        time.perf_counter() - suspend_started
                    )

                    resume_started = time.perf_counter()
                    resumed = sandbox.run_code(
                        "from pathlib import Path; "
                        "print(sum(p.stat().st_size for p in "
                        "Path('/workspace/resume-data').glob('*.bin')))"
                    )
                    results[size_mib]["resumeToCode"].append(
                        time.perf_counter() - resume_started
                    )
                    if not resumed.success or int(resumed.stdout.strip()) != (
                        size_mib * 1024 * 1024
                    ):
                        raise RuntimeError(f"restore verification failed: {resumed}")
                finally:
                    sandbox.delete()
                print(
                    f"completed {size_mib} MiB round {round_number + 1}/{args.rounds}",
                    flush=True,
                )

    output = {
        f"{size_mib}MiB": {
            metric: summary(samples) for metric, samples in measurements.items()
        }
        for size_mib, measurements in results.items()
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
