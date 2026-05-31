"""Code runner for Jupyter kernel execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prokube.sandbox.models import CodeResult

if TYPE_CHECKING:
    from prokube.sandbox.client import SandboxClient


class CodeRunner:
    """Runner for code execution using Jupyter kernel.

    This class provides stateful code execution - variables and imports
    persist between calls, similar to running cells in a Jupyter notebook.

    The session_id is automatically managed: the first call creates a new
    session, and subsequent calls reuse the same session for state persistence.

    Example:
        >>> sandbox.run_code("import pandas as pd")
        >>> sandbox.run_code("df = pd.DataFrame({'a': [1, 2, 3]})")
        >>> result = sandbox.run_code("print(df.sum())")
        >>> print(result.stdout)
        a    6
        dtype: int64
    """

    def __init__(
        self,
        client: SandboxClient,
        sandbox_name: str,
        check_killed: Callable[[], None] | None = None,
    ) -> None:
        """Initialize code runner.

        Args:
            client: Sandbox API client.
            sandbox_name: Name of the sandbox.
            check_killed: Optional callback to check if sandbox is killed.
        """
        self._client = client
        self._sandbox_name = sandbox_name
        self._session_id: str | None = None
        self._reset_on_next_exec: bool = False
        self._check_killed = check_killed

    def run(
        self,
        code: str,
        language: str = "python",
        timeout: int = 300,
    ) -> CodeResult:
        """Execute code in the Jupyter kernel.

        The kernel maintains state between calls - variables and imports
        persist. The session is automatically managed.

        Args:
            code: Code to execute.
            language: Programming language (default: python).
            timeout: Timeout in seconds (default: 300).

        Returns:
            CodeResult with stdout, stderr, success, execution_time_ms,
            and error information if execution failed.

        Example:
            >>> result = sandbox.run_code("print(2 + 2)")
            >>> print(result.stdout)  # "4"
            >>> print(result.success)  # True

            >>> result = sandbox.run_code("raise ValueError('oops')")
            >>> print(result.success)  # False
            >>> print(result.error_name)  # "ValueError"
            >>> print(result.error_value)  # "oops"
        """
        if self._check_killed:
            self._check_killed()

        # Check if we need to reset the session
        reset_session = self._reset_on_next_exec

        try:
            result = self._client.exec_code(
                name=self._sandbox_name,
                code=code,
                language=language,
                timeout=timeout,
                session_id=self._session_id,
                reset_session=reset_session,
            )
        except Exception:
            # Don't clear reset flag on failure so next call can retry
            raise
        else:
            # Only clear reset flag after successful execution
            self._reset_on_next_exec = False

        # Store session_id for subsequent calls to maintain state
        if result.session_id:
            self._session_id = result.session_id
        return result

    def reset_session(self) -> None:
        """Reset the Jupyter kernel session.

        The next run_code() call will restart the kernel and clear all
        variables and imports from previous executions.
        """
        self._session_id = None
        self._reset_on_next_exec = True

    @property
    def session_id(self) -> str | None:
        """Get the current session ID, if any."""
        return self._session_id
