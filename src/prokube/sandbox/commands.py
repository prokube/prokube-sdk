"""Command runner for sandbox shell commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from prokube.sandbox.models import CommandResult

if TYPE_CHECKING:
    from prokube.sandbox.client import SandboxClient


class CommandRunner:
    """Runner for shell commands in a sandbox.

    This class provides a convenient interface for executing shell commands
    in a sandbox environment.

    Example:
        >>> result = sandbox.commands.run("pip install pandas")
        >>> print(result.exit_code)
        0
        >>> print(result.stdout)
        Successfully installed pandas-2.0.0
    """

    def __init__(
        self,
        client: SandboxClient,
        sandbox_name: str,
        check_killed: Callable[[], None] | None = None,
    ) -> None:
        """Initialize command runner.

        Args:
            client: Sandbox API client.
            sandbox_name: Name of the sandbox.
            check_killed: Optional callback to check if sandbox is killed.
        """
        self._client = client
        self._sandbox_name = sandbox_name
        self._check_killed = check_killed

    def run(self, command: str, timeout: int = 300) -> CommandResult:
        """Execute a shell command in the sandbox.

        Args:
            command: Shell command to execute.
            timeout: Timeout in seconds (default: 300).

        Returns:
            CommandResult with stdout, stderr, exit_code, and duration_ms.

        Example:
            >>> result = sandbox.commands.run("ls -la /workspace")
            >>> if result.success:
            ...     print(result.stdout)
            ... else:
            ...     print(f"Failed: {result.stderr}")
        """
        if self._check_killed:
            self._check_killed()
        return self._client.exec_command(
            name=self._sandbox_name,
            command=command,
            timeout=timeout,
        )
