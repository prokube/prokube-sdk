#!/usr/bin/env python3
"""Upload and verify a large file set through the v0.1 run_code channel."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
import time
import uuid

from prokube import Sandbox, SandboxClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.environ.get("PROKUBE_API_URL"))
    parser.add_argument("--workspace", default=os.environ.get("PROKUBE_WORKSPACE"))
    parser.add_argument("--api-key", default=os.environ.get("PROKUBE_API_KEY"))
    parser.add_argument("--user-id", default=os.environ.get("PROKUBE_USER_ID"))
    parser.add_argument("--files", type=int, default=10_000)
    parser.add_argument("--bytes-per-file", type=int, default=128)
    parser.add_argument("--suspend-command")
    args = parser.parse_args()
    if not args.endpoint or not args.workspace:
        parser.error("--endpoint and --workspace are required")
    if args.files < 1 or args.bytes_per_file < 1:
        parser.error("--files and --bytes-per-file must be positive")
    return args


def file_content(index: int, size: int) -> bytes:
    content = bytearray()
    chunk = 0
    while len(content) < size:
        content.extend(hashlib.sha256(f"{index}:{chunk}".encode()).digest())
        chunk += 1
    return bytes(content[:size])


def build_archive(file_count: int, bytes_per_file: int) -> tuple[bytes, str]:
    output = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=output, mode="w:gz", compresslevel=6) as archive:
        for index in range(file_count):
            content = file_content(index, bytes_per_file)
            digest.update(content)
            info = tarfile.TarInfo(f"{index // 1000:02d}/file-{index:05d}.bin")
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue(), digest.hexdigest()


def timed_run_code(sandbox: Sandbox, code: str) -> tuple[float, str]:
    started = time.perf_counter()
    result = sandbox.run_code(code, timeout=300)
    elapsed = time.perf_counter() - started
    if not result.success:
        raise RuntimeError(f"{result.error_name}: {result.error_value}")
    return elapsed, result.stdout.strip()


def main() -> None:
    args = parse_args()
    archive_started = time.perf_counter()
    archive, expected_digest = build_archive(args.files, args.bytes_per_file)
    archive_seconds = time.perf_counter() - archive_started
    encoded = base64.b64encode(archive).decode()
    destination = "/workspace/load-test-10000"
    upload_code = (
        "import base64, io, tarfile; "
        f"data=base64.b64decode({encoded!r}); "
        f"archive=tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); "
        f"archive.extractall({destination!r}, filter='data'); archive.close(); "
        "print(len(data))"
    )
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
        sandbox = client.create(name=name)
        try:
            upload_seconds, uploaded_bytes = timed_run_code(sandbox, upload_code)
            verify_seconds, raw_verification = timed_run_code(sandbox, verify_code)
            verification = json.loads(raw_verification)
            if verification != {
                "files": args.files,
                "bytes": args.files * args.bytes_per_file,
                "digest": expected_digest,
            }:
                raise RuntimeError(f"verification failed: {verification}")

            resume_seconds = None
            if args.suspend_command:
                command = shlex.split(args.suspend_command.format(name=name))
                subprocess.run(command, check=True)
                resume_seconds, raw_after_resume = timed_run_code(sandbox, verify_code)
                if json.loads(raw_after_resume) != verification:
                    raise RuntimeError("file set changed across suspend/resume")

            print(
                json.dumps(
                    {
                        "sandbox": name,
                        "files": args.files,
                        "uncompressedBytes": args.files * args.bytes_per_file,
                        "archiveBytes": len(archive),
                        "encodedBytes": len(encoded),
                        "guestReportedArchiveBytes": int(uploaded_bytes),
                        "archiveBuildSeconds": round(archive_seconds, 3),
                        "uploadAndExtractSeconds": round(upload_seconds, 3),
                        "verifySeconds": round(verify_seconds, 3),
                        "resumeAndVerifySeconds": (
                            round(resume_seconds, 3)
                            if resume_seconds is not None
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
