#!/usr/bin/env python3
"""Prove request parking when all Substrate workers are assigned."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from prokube import SandboxClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.environ.get("PROKUBE_API_URL"))
    parser.add_argument("--workspace", default=os.environ.get("PROKUBE_WORKSPACE"))
    parser.add_argument("--api-key", default=os.environ.get("PROKUBE_API_KEY"))
    parser.add_argument("--user-id", default=os.environ.get("PROKUBE_USER_ID"))
    parser.add_argument("--observe-seconds", type=float, default=3)
    parser.add_argument("--suspend-command", required=True)
    args = parser.parse_args()
    if not args.endpoint or not args.workspace:
        parser.error("--endpoint and --workspace are required")
    return args


def main() -> None:
    args = parse_args()
    suffix = uuid.uuid4().hex[:8]
    with SandboxClient(
        endpoint=args.endpoint,
        workspace=args.workspace,
        api_key=args.api_key,
        user_id=None if args.api_key else args.user_id,
        timeout=360,
    ) as client:
        first = client.create(name=f"capacity-a-{suffix}")
        second = client.create(name=f"capacity-b-{suffix}")
        try:
            active = first.run_code("owner = 'first'; print(owner)")
            if not active.success:
                raise RuntimeError(active.error_value)

            with ThreadPoolExecutor(max_workers=1) as executor:
                started = time.perf_counter()
                waiting = executor.submit(
                    second.run_code, "owner = 'second'; print(owner)"
                )
                time.sleep(args.observe_seconds)
                parked = not waiting.done()

                command = shlex.split(args.suspend_command.format(name=first.name))
                suspend_started = time.perf_counter()
                subprocess.run(command, check=True)
                suspend_seconds = time.perf_counter() - suspend_started

                result = waiting.result(timeout=300)
                total_wait_seconds = time.perf_counter() - started
                if not result.success or result.stdout.strip() != "second":
                    raise RuntimeError(
                        f"second Sandbox did not execute successfully: {result}"
                    )

            print(f"firstSandbox={first.name}")
            print(f"secondSandbox={second.name}")
            print(f"parkedAfter{args.observe_seconds:g}Seconds={str(parked).lower()}")
            print(f"suspendSeconds={suspend_seconds:.3f}")
            print(f"secondRequestTotalSeconds={total_wait_seconds:.3f}")
            print(f"secondSession={result.session_id}")
        finally:
            first.delete()
            second.delete()


if __name__ == "__main__":
    main()
