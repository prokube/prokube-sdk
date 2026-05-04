"""Tests for Pydantic models."""

import pytest

from prokube.sandbox.client import _parse_batch_file_write_response
from prokube.sandbox.models import (
    BatchFileWriteResponse,
    ClaimRequest,
    CodeResult,
    CommandResult,
    CreateRequest,
    ExecRequest,
    FileInfo,
    FileWriteRequest,
    SandboxInfo,
    SandboxStatus,
)


class TestSandboxStatus:
    """Tests for SandboxStatus enum."""

    def test_status_values(self):
        """Test status enum values."""
        assert SandboxStatus.PENDING.value == "Pending"
        assert SandboxStatus.RUNNING.value == "Running"
        assert SandboxStatus.BOUND.value == "Bound"
        assert SandboxStatus.SUCCEEDED.value == "Succeeded"
        assert SandboxStatus.FAILED.value == "Failed"
        assert SandboxStatus.UNKNOWN.value == "Unknown"


class TestSandboxInfo:
    """Tests for SandboxInfo model."""

    def test_minimal_sandbox_info(self):
        """Test creating SandboxInfo with minimal fields."""
        info = SandboxInfo(name="test-sandbox", workspace="test-ws")
        assert info.name == "test-sandbox"
        assert info.workspace == "test-ws"
        assert info.status == SandboxStatus.UNKNOWN
        assert info.image is None
        assert info.pool is None

    def test_full_sandbox_info(self):
        """Test creating SandboxInfo with all fields."""
        info = SandboxInfo(
            name="test-sandbox",
            workspace="test-ws",
            status=SandboxStatus.RUNNING,
            image="python:3.10",
            pool="my-pool",
            created_at="2024-01-01T00:00:00Z",
        )
        assert info.status == SandboxStatus.RUNNING
        assert info.image == "python:3.10"
        assert info.pool == "my-pool"


class TestCommandResult:
    """Tests for CommandResult model."""

    def test_successful_command(self):
        """Test successful command result."""
        result = CommandResult(
            stdout="hello world",
            stderr="",
            exit_code=0,
            duration_ms=100,
        )
        assert result.success is True
        assert result.stdout == "hello world"
        assert result.exit_code == 0

    def test_failed_command(self):
        """Test failed command result."""
        result = CommandResult(
            stdout="",
            stderr="command not found",
            exit_code=127,
            duration_ms=50,
        )
        assert result.success is False
        assert result.exit_code == 127


class TestCodeResult:
    """Tests for CodeResult model."""

    def test_successful_code_result(self):
        """Test successful code execution result."""
        result = CodeResult(
            stdout="42\n",
            stderr="",
            success=True,
            execution_time_ms=50,
        )
        assert result.success is True
        assert result.stdout == "42\n"
        assert result.error_name is None

    def test_failed_code_result(self):
        """Test failed code execution result."""
        result = CodeResult(
            stdout="",
            stderr="",
            success=False,
            execution_time_ms=10,
            error_name="ValueError",
            error_value="invalid value",
            traceback=["Traceback...", "  File..."],
        )
        assert result.success is False
        assert result.error_name == "ValueError"
        assert result.error_value == "invalid value"
        assert len(result.traceback) == 2


class TestFileInfo:
    """Tests for FileInfo model."""

    def test_file_info(self):
        """Test file info for a regular file."""
        info = FileInfo(
            name="test.txt",
            path="/workspace/test.txt",
            is_dir=False,
            size=1024,
            modified="2024-01-01T00:00:00Z",
        )
        assert info.name == "test.txt"
        assert info.is_dir is False
        assert info.size == 1024

    def test_directory_info(self):
        """Test file info for a directory."""
        info = FileInfo(
            name="subdir",
            path="/workspace/subdir",
            is_dir=True,
        )
        assert info.is_dir is True
        assert info.size == 0  # default


class TestRequestModels:
    """Tests for request models."""

    def test_exec_request_defaults(self):
        """Test ExecRequest with defaults."""
        req = ExecRequest(code="print('hello')")
        assert req.use_jupyter is True
        assert req.timeout == 300
        assert req.language == "python"

    def test_exec_request_custom(self):
        """Test ExecRequest with custom values."""
        req = ExecRequest(
            code="ls -la",
            use_jupyter=False,
            timeout=60,
        )
        assert req.use_jupyter is False
        assert req.timeout == 60

    def test_claim_request(self):
        """Test ClaimRequest."""
        req = ClaimRequest(pool_name="python-pool")
        assert req.pool_name == "python-pool"
        # Check it serializes with camelCase alias
        assert req.model_dump(by_alias=True) == {"poolName": "python-pool"}

    def test_create_request(self):
        """Test CreateRequest."""
        req = CreateRequest(image="python:3.10", name="my-sandbox")
        assert req.image == "python:3.10"
        assert req.name == "my-sandbox"

    def test_file_write_request(self):
        """Test FileWriteRequest."""
        req = FileWriteRequest(
            path="/workspace/test.txt",
            content="aGVsbG8gd29ybGQ=",  # "hello world" base64-encoded
        )
        assert req.path == "/workspace/test.txt"
        assert req.content == "aGVsbG8gd29ybGQ="
        # The encoding field defaults to "base64" so the backend knows to
        # decode the content before forwarding to execd. Without this the
        # backend treats it as text and writes the literal base64 string
        # to disk (issue #18).
        assert req.encoding == "base64"
        assert req.model_dump()["encoding"] == "base64"

    def test_batch_file_write_response_preserves_result_fields(self):
        """Batch response model keeps the per-file payload intact."""
        response = BatchFileWriteResponse(
            success=False,
            total=2,
            success_count=1,
            failure_count=1,
            results=[
                {"index": 1, "path": "/workspace/b.txt", "success": False},
                {"index": 0, "path": "/workspace/a.txt", "success": True},
            ],
        )

        assert [item.index for item in response.results] == [1, 0]
        assert response.results[1].path == "/workspace/a.txt"


class TestBatchFileWriteResponseParsing:
    """Tests for batch write response normalization helpers."""

    def test_parse_batch_response_sorts_results_by_index(self):
        response = _parse_batch_file_write_response(
            {
                "success": True,
                "total": 2,
                "results": [
                    {"index": 1, "path": "/workspace/b.txt", "success": True},
                    {"index": 0, "path": "/workspace/a.txt", "success": True},
                ],
            }
        )

        assert [item.index for item in response.results] == [0, 1]
        assert [item.path for item in response.results] == [
            "/workspace/a.txt",
            "/workspace/b.txt",
        ]
        assert response.success_count == 2
        assert response.failure_count == 0

    def test_parse_batch_response_supports_legacy_counts(self):
        response = _parse_batch_file_write_response(
            {
                "success": False,
                "total": 2,
                "success_count": 1,
                "failure_count": 1,
                "results": [
                    {"index": 0, "path": "/workspace/a.txt", "success": True},
                    {
                        "index": 1,
                        "path": "/workspace/b.txt",
                        "success": False,
                        "error": "disk full",
                    },
                ],
            }
        )

        assert response.success_count == 1
        assert response.failure_count == 1
        assert response.results[1].error == "disk full"

    def test_parse_batch_response_rejects_missing_required_fields(self):
        with pytest.raises(ValueError, match="missing path"):
            _parse_batch_file_write_response(
                {
                    "success": True,
                    "results": [{"index": 0, "success": True}],
                }
            )

    def test_parse_batch_response_rejects_non_object_entries(self):
        with pytest.raises(ValueError, match="must be an object"):
            _parse_batch_file_write_response(
                {
                    "success": True,
                    "results": ["not-an-object"],
                }
            )

    def test_parse_batch_response_rejects_non_boolean_success(self):
        with pytest.raises(ValueError, match="boolean success"):
            _parse_batch_file_write_response(
                {
                    "success": "false",
                    "results": [
                        {"index": 0, "path": "/workspace/a.txt", "success": True}
                    ],
                }
            )

    def test_parse_batch_response_rejects_boolean_index(self):
        with pytest.raises(ValueError, match="missing index"):
            _parse_batch_file_write_response(
                {
                    "success": True,
                    "results": [
                        {"index": True, "path": "/workspace/a.txt", "success": True}
                    ],
                }
            )
