"""Live contract tests for a deployed greenfield pk-sandbox backend."""

import os
import uuid

import pytest

from prokube import SandboxClient

pytestmark = pytest.mark.skipif(
    os.environ.get("PROKUBE_E2E") != "1",
    reason="set PROKUBE_E2E=1 to run deployment tests",
)


def test_create_stateful_code_and_delete() -> None:
    name = f"sdk-e2e-{uuid.uuid4().hex[:10]}"
    api_key = os.environ.get("PROKUBE_API_KEY")
    with SandboxClient(
        endpoint=os.environ["PROKUBE_API_URL"],
        workspace=os.environ["PROKUBE_WORKSPACE"],
        api_key=api_key,
        user_id=None if api_key else os.environ.get("PROKUBE_USER_ID"),
        timeout=360,
    ) as client:
        sandbox = client.create(name=name)
        try:
            first = sandbox.run_code("value = 41; print(value)", timeout=300)
            second = sandbox.run_code("value += 1; print(value)", timeout=300)
            assert first.success and first.stdout.strip() == "41"
            assert second.success and second.stdout.strip() == "42"
            assert first.session_id == second.session_id
            sandbox.files.write("/workspace/binary.dat", b"\x00\x01\xff")
            batch = sandbox.files.write_batch(
                [("/workspace/a.txt", "hello"), ("/workspace/b.txt", b"world")]
            )
            assert batch.success and batch.success_count == 2
            assert sandbox.files.read("/workspace/binary.dat") == b"\x00\x01\xff"
            assert {entry.name for entry in sandbox.files.list()} >= {
                "a.txt",
                "b.txt",
                "binary.dat",
            }
        finally:
            sandbox.delete()
            sandbox.delete()
