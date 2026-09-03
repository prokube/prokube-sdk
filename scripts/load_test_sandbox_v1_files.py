#!/usr/bin/env python3
"""Upload and verify a large file set through the v0.1 files API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid

from prokube import Sandbox, SandboxClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.environ.get("PROKUBE_API_URL"))
    parser.add_argument("--workspace", default=os.environ.get("PROKUBE_WORKSPACE"))
    parser.add_argument("--api-key", default=os.environ.get("PROKUBE_API_KEY"))
    parser.add_argument("--user-id", default=os.environ.get("PROKUBE_USER_ID"))
    parser.add_argument(
        "--runtime", choices=("python", "python-microvm"), default="python"
    )
    parser.add_argument("--files", type=int, default=10_000)
    parser.add_argument("--bytes-per-file", type=int, default=128)
    parser.add_argument("--download-samples", type=int, default=100)
    parser.add_argument("--suspend-command")
    args = parser.parse_args()
    if not args.endpoint or not args.workspace:
        parser.error("--endpoint and --workspace are required")
    if args.files < 1 or args.bytes_per_file < 1 or args.download_samples < 0:
        parser.error(
            "file counts and sizes must be non-negative, with at least one file"
        )
    return args


def file_content(index: int, size: int) -> bytes:
    content = bytearray()
    chunk = 0
    while len(content) < size:
        content.extend(hashlib.sha256(f"{index}:{chunk}".encode()).digest())
        chunk += 1
    return bytes(content[:size])


def build_files(
    file_count: int, bytes_per_file: int
) -> tuple[list[tuple[str, bytes]], str]:
    files = []
    digest = hashlib.sha256()
    for index in range(file_count):
        content = file_content(index, bytes_per_file)
        digest.update(content)
        files.append(
            (
                f"/workspace/load-test-10000/{index // 1000:02d}/file-{index:05d}.bin",
                content,
            )
        )
    return files, digest.hexdigest()


def timed_run_code(sandbox: Sandbox, code: str) -> tuple[float, str]:
    started = time.perf_counter()
    result = sandbox.run_code(code, timeout=300)
    elapsed = time.perf_counter() - started
    if not result.success:
        raise RuntimeError(f"{result.error_name}: {result.error_value}")
    return elapsed, result.stdout.strip()


def main() -> None:
    args = parse_args()
    build_started = time.perf_counter()
    files, expected_digest = build_files(args.files, args.bytes_per_file)
    build_seconds = time.perf_counter() - build_started
    destination = "/workspace/load-test-10000"
    verify_code = (
        "from pathlib import Path; import hashlib, json; "
        f"files=sorted(Path({destination!r}).rglob('*.bin')); "
        "digest=hashlib.sha256(); [digest.update(path.read_bytes()) for path in files]; "
        "print(json.dumps({'files':len(files),'bytes':sum(path.stat().st_size for path in files),"
        "'digest':digest.hexdigest()}))"
    )

    name = f"sdk-files-{uuid.uuid4().hex[:10]}"
    with SandboxClient(
        endpoint=args.endpoint,
        workspace=args.workspace,
        api_key=args.api_key,
        user_id=None if args.api_key else args.user_id,
        timeout=360,
    ) as client:
        sandbox = client.create(name=name, runtime=args.runtime)
        try:
            upload_started = time.perf_counter()
            for offset in range(0, len(files), 100):
                result = sandbox.files.write_batch(files[offset : offset + 100])
                if not result.success:
                    failures = [item for item in result.results if not item.success]
                    raise RuntimeError(f"batch upload failed: {failures[:3]}")
            upload_seconds = time.perf_counter() - upload_started

            sample_count = min(args.download_samples, len(files))
            download_started = time.perf_counter()
            for path, expected in files[:sample_count]:
                if sandbox.files.read(path) != expected:
                    raise RuntimeError(f"download verification failed: {path}")
            download_seconds = time.perf_counter() - download_started

            verify_seconds, raw_verification = timed_run_code(sandbox, verify_code)
            verification = json.loads(raw_verification)
            if verification != {
                "files": args.files,
                "bytes": args.files * args.bytes_per_file,
                "digest": expected_digest,
            }:
                raise RuntimeError(f"verification failed: {verification}")

            suspend_seconds = None
            resume_read_seconds = None
            verify_after_resume_seconds = None
            if args.suspend_command:
                command = shlex.split(args.suspend_command.format(name=name))
                suspend_started = time.perf_counter()
                subprocess.run(command, check=True)
                suspend_seconds = time.perf_counter() - suspend_started
                resume_started = time.perf_counter()
                if sandbox.files.read(files[0][0]) != files[0][1]:
                    raise RuntimeError("file changed across suspend/resume")
                resume_read_seconds = time.perf_counter() - resume_started
                verify_after_resume_seconds, raw_after_resume = timed_run_code(
                    sandbox, verify_code
                )
                if json.loads(raw_after_resume) != verification:
                    raise RuntimeError("file set changed across suspend/resume")

            print(
                json.dumps(
                    {
                        "sandbox": name,
                        "files": args.files,
                        "uncompressedBytes": args.files * args.bytes_per_file,
                        "batches": (args.files + 99) // 100,
                        "downloadSamples": sample_count,
                        "fixtureBuildSeconds": round(build_seconds, 3),
                        "batchUploadSeconds": round(upload_seconds, 3),
                        "sampleDownloadSeconds": round(download_seconds, 3),
                        "verifySeconds": round(verify_seconds, 3),
                        "suspendSeconds": (
                            round(suspend_seconds, 3)
                            if suspend_seconds is not None
                            else None
                        ),
                        "resumeFirstReadSeconds": (
                            round(resume_read_seconds, 3)
                            if resume_read_seconds is not None
                            else None
                        ),
                        "verifyAfterResumeSeconds": (
                            round(verify_after_resume_seconds, 3)
                            if verify_after_resume_seconds is not None
                            else None
                        ),
                        "digest": expected_digest,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        finally:
            sandbox.delete()


if __name__ == "__main__":
    main()
