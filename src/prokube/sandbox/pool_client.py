"""HTTP client for sandbox pool API operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prokube.common.compat import check_backend_compatibility
from prokube.common.http import HttpClient
from prokube.sandbox.models import CreatePoolRequest, PoolInfo

if TYPE_CHECKING:
    from prokube.common.config import Config


class PoolClient:
    """Client for sandbox pool API operations."""

    def __init__(self, config: Config, check_version: bool = True) -> None:
        """Initialize pool client.

        Args:
            config: SDK configuration.
            check_version: Whether to check backend version compatibility.
        """
        self.config = config
        self._http = HttpClient(config)

        if check_version:
            check_backend_compatibility(self._http)

    def close(self) -> None:
        """Close the client."""
        self._http.close()

    def _pools_path(self) -> str:
        """Get API path for the pools collection."""
        ws = self.config.workspace
        if self.config.use_api_key:
            return f"/sandbox/{ws}/sandbox-pools"
        return f"/api/namespaces/{ws}/sandbox-pools"

    def _pool_path(self, name: str) -> str:
        """Get API path for a specific pool."""
        return f"{self._pools_path()}/{name}"

    def create_pool(
        self,
        name: str,
        image: str,
        pool_size: int,
        cpu: str,
        memory: str,
    ) -> PoolInfo:
        """Create a new sandbox pool.

        Args:
            name: Pool name.
            image: Container image to use.
            pool_size: Number of warm sandboxes to maintain.
            cpu: CPU resource request (e.g. '2').
            memory: Memory resource request (e.g. '4Gi').

        Returns:
            Information about the created pool.
        """
        request = CreatePoolRequest(
            name=name, image=image, pool_size=pool_size, cpu=cpu, memory=memory
        )
        response = self._http.post(
            self._pools_path(),
            json=request.model_dump(by_alias=True, exclude_none=True),
        )
        status = response.get("status", {})
        return PoolInfo(
            name=response.get("name", name),
            workspace=self.config.workspace,
            replicas=response.get("replicas", response.get("poolSize", pool_size)),
            ready_replicas=status.get(
                "warmPods", status.get("availablePods", response.get("readyReplicas", 0))
            ),
            image=response.get("image", image),
            cpu=response.get("cpu", cpu),
            memory=response.get("memory", memory),
        )

    def list_pools(self) -> list[PoolInfo]:
        """List all sandbox pools in the configured workspace.

        Returns:
            List of pool info objects.
        """
        response = self._http.get(self._pools_path())
        pools = response.get("pools", [])
        result = []
        for p in pools:
            s = p.get("status", {})
            result.append(
                PoolInfo(
                    name=p["name"],
                    workspace=self.config.workspace,
                    replicas=p.get("replicas", p.get("poolSize", 0)),
                    ready_replicas=s.get(
                        "warmPods", s.get("availablePods", p.get("readyReplicas", 0))
                    ),
                    image=p.get("image"),
                    cpu=p.get("cpu"),
                    memory=p.get("memory"),
                )
            )
        return result

    def get_pool(self, name: str) -> PoolInfo:
        """Get information about a sandbox pool.

        Args:
            name: Pool name.

        Returns:
            Information about the pool.
        """
        response = self._http.get(self._pool_path(name))
        status = response.get("status", {})
        return PoolInfo(
            name=response["name"],
            workspace=self.config.workspace,
            replicas=response.get("replicas", response.get("poolSize", 0)),
            ready_replicas=status.get(
                "warmPods", status.get("availablePods", response.get("readyReplicas", 0))
            ),
            image=response.get("image"),
            cpu=response.get("cpu"),
            memory=response.get("memory"),
        )

    def delete_pool(self, name: str) -> None:
        """Delete a sandbox pool.

        Args:
            name: Pool name.
        """
        self._http.delete(self._pool_path(name))
