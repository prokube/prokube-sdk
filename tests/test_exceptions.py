"""Tests for exception classes."""

from prokube.common.exceptions import (
    AuthenticationError,
    NotFoundError,
    PoolExhaustedError,
    PoolNotFoundError,
    ProKubeError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)


class TestExceptions:
    """Tests for exception hierarchy."""

    def test_prokube_error_is_base(self):
        """Test that ProKubeError is the base exception."""
        error = ProKubeError("test error")
        assert isinstance(error, Exception)
        assert str(error) == "test error"

    def test_authentication_error_inherits_from_prokube(self):
        """Test AuthenticationError inherits from ProKubeError."""
        error = AuthenticationError("auth failed")
        assert isinstance(error, ProKubeError)
        assert isinstance(error, Exception)

    def test_sandbox_error_inherits_from_prokube(self):
        """Test SandboxError inherits from ProKubeError."""
        error = SandboxError("sandbox failed")
        assert isinstance(error, ProKubeError)

    def test_not_found_error_inherits_from_prokube(self):
        """Test NotFoundError inherits from ProKubeError."""
        error = NotFoundError("not found")
        assert isinstance(error, ProKubeError)

    def test_sandbox_not_found_inherits_from_both(self):
        """Test SandboxNotFoundError inherits from SandboxError and NotFoundError."""
        error = SandboxNotFoundError("not found")
        assert isinstance(error, SandboxError)
        assert isinstance(error, NotFoundError)
        assert isinstance(error, ProKubeError)

    def test_sandbox_timeout_inherits_from_sandbox(self):
        """Test SandboxTimeoutError inherits from SandboxError."""
        error = SandboxTimeoutError("timeout")
        assert isinstance(error, SandboxError)

    def test_sandbox_execution_error_inherits_from_sandbox(self):
        """Test SandboxExecutionError inherits from SandboxError."""
        error = SandboxExecutionError("execution failed")
        assert isinstance(error, SandboxError)

    def test_pool_not_found_inherits_from_sandbox(self):
        """Test PoolNotFoundError inherits from SandboxError."""
        error = PoolNotFoundError("pool not found")
        assert isinstance(error, SandboxError)

    def test_pool_exhausted_inherits_from_sandbox(self):
        """Test PoolExhaustedError inherits from SandboxError."""
        error = PoolExhaustedError("pool exhausted")
        assert isinstance(error, SandboxError)
