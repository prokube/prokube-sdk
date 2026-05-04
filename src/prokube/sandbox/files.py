"""File manager for sandbox file operations."""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from prokube.sandbox.models import (
    MAX_BATCH_WRITE_ITEMS,
    BatchFileWriteResponse,
    FileInfo,
    FileWriteRequest,
)

if TYPE_CHECKING:
    from prokube.sandbox.client import SandboxClient


class FileManager:
    """Manager for file operations in a sandbox.

    This class provides a convenient interface for uploading, downloading,
    and listing files in a sandbox environment.

    Example:
        >>> sandbox.files.write("/workspace/data.csv", b"col1,col2\\n1,2")
        >>> content = sandbox.files.read("/workspace/data.csv")
        >>> files = sandbox.files.list("/workspace")
    """

    def __init__(
        self,
        client: SandboxClient,
        sandbox_name: str,
        check_killed: Callable[[], None] | None = None,
    ) -> None:
        """Initialize file manager.

        Args:
            client: Sandbox API client.
            sandbox_name: Name of the sandbox.
            check_killed: Optional callback to check if sandbox is killed.
        """
        self._client = client
        self._sandbox_name = sandbox_name
        self._check_killed = check_killed

    def write(self, path: str, content: bytes | str) -> None:
        """Upload a file to the sandbox.

        Args:
            path: Absolute path where to write the file in the sandbox.
            content: File content as bytes or string.
                     Strings are encoded as UTF-8.

        Example:
            >>> # Write binary data
            >>> sandbox.files.write("/workspace/image.png", image_bytes)
            >>> # Write text data
            >>> sandbox.files.write("/workspace/script.py", "print('hello')")
        """
        if self._check_killed:
            self._check_killed()
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._client.write_file(
            name=self._sandbox_name,
            path=path,
            content=content,
        )

    def read(self, path: str) -> bytes:
        """Download a file from the sandbox.

        Args:
            path: Absolute path to the file in the sandbox.

        Returns:
            File content as bytes.

        Example:
            >>> content = sandbox.files.read("/workspace/output.txt")
            >>> print(content.decode("utf-8"))
        """
        if self._check_killed:
            self._check_killed()
        return self._client.read_file(
            name=self._sandbox_name,
            path=path,
        )

    def write_batch(
        self, items: Sequence[tuple[str, bytes | str]]
    ) -> BatchFileWriteResponse:
        """Upload multiple files to the sandbox in request order.

        Args:
            items: Ordered sequence of ``(path, content)`` pairs. String
                content is encoded as UTF-8 before upload.
        """
        if self._check_killed:
            self._check_killed()

        if len(items) > MAX_BATCH_WRITE_ITEMS:
            raise ValueError(
                f"Batch write supports at most {MAX_BATCH_WRITE_ITEMS} items"
            )

        requests: list[FileWriteRequest] = []
        for path, content in items:
            if isinstance(content, str):
                content = content.encode("utf-8")
            requests.append(
                FileWriteRequest(
                    path=path,
                    content=base64.b64encode(content).decode("ascii"),
                    encoding="base64",
                )
            )

        return self._client.write_files_batch(
            name=self._sandbox_name,
            items=requests,
        )

    def list(self, path: str = "/workspace") -> list[FileInfo]:
        """List files in a directory.

        Args:
            path: Directory path to list (default: /workspace).

        Returns:
            List of FileInfo objects with name, path, is_dir, size, modified.

        Example:
            >>> files = sandbox.files.list("/workspace")
            >>> for f in files:
            ...     print(f"{f.name} ({f.size} bytes)")
        """
        if self._check_killed:
            self._check_killed()
        return self._client.list_files(
            name=self._sandbox_name,
            path=path,
        )
