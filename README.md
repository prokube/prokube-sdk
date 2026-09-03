# prokube-sdk

Python SDK for the prokube.ai platform.

## Greenfield Sandbox v0.1

The top-level `SandboxClient` implements the deliberately small, incompatible
v0.1 API: create, stateful Python execution, file operations, and idempotent delete. Actor
activation, pause, suspend, and resume remain transparent runtime details.

```python
from prokube import SandboxClient

with SandboxClient(
    endpoint="https://platform.example.com/pkui",
    workspace="research",
    api_key="...",
) as client:
    with client.create(
        name="research-task-42",
        runtime="python",
        size="small",
        network="offline",
    ) as sandbox:
        sandbox.run_code("value = 41")
        result = sandbox.run_code("value += 1; print(value)")
        print(result.stdout)
        sandbox.files.write("/workspace/input.bin", b"\x00\x01")
        assert sandbox.files.read("/workspace/input.bin") == b"\x00\x01"
        files = sandbox.files.list("/workspace")
```

The older `prokube.sandbox.Sandbox` API remains available for deployments using
the pre-greenfield backend, but its pool, command, and explicit lifecycle
methods are not supported by the v0.1 backend.

Run the opt-in deployment test with:

```bash
PROKUBE_E2E=1 \
PROKUBE_API_URL=http://127.0.0.1:18080 \
PROKUBE_WORKSPACE=research \
PROKUBE_USER_ID=user@example.com \
uv run pytest tests/e2e/test_sandbox_v1_live.py -v
```

Measure lazy create, cold first-code, active code, a 64 KiB code payload,
1 KiB and 1 MiB file API I/O, delete, and optionally transparent
resume-to-code with:

```bash
uv run python scripts/benchmark_sandbox_v1.py \
  --endpoint http://127.0.0.1:18080 \
  --workspace research \
  --user-id user@example.com \
  --rounds 10
```

The public `sandbox.files` API supports single and batch upload, binary download,
and directory listing. `--suspend-command` accepts a local command with a
`{name}` placeholder when transparent resume-to-code should also be measured.

`scripts/load_test_sandbox_v1_files.py` uploads 10,000 deterministic files in
100-item API batches, samples binary downloads, verifies count, byte size, and
aggregate digest, and can repeat verification after an external suspend.
`scripts/load_test_sandbox_v1_capacity.py` keeps one Actor active, starts a
second Actor request, proves that it remains parked, pauses or suspends the
first Actor, and measures how long the second request takes to acquire the
released worker. Both pause and suspend clear the worker assignment; pause
retains a node-local snapshot while suspend uploads a durable snapshot.

## Installation

```bash
# From source with uv
uv pip install git+https://github.com/prokube/prokube-sdk.git

# Or with pip
pip install git+https://github.com/prokube/prokube-sdk.git

# For development
git clone https://github.com/prokube/prokube-sdk.git
cd prokube-sdk
uv sync --dev
```

## Quick Start

```python
from prokube.sandbox import Sandbox

# Claim a sandbox from a warm pool (fast, but adoption is asynchronous)
sbx = Sandbox.from_pool("python-pool")
sbx.wait_until_ready()

# Or create directly (cold start, ~10-30s)
sbx = Sandbox.create(image="pk-sandbox:python-datascience")
sbx.wait_until_ready()

# Execute code (stateful - variables persist between calls)
sbx.run_code("import pandas as pd")
sbx.run_code("df = pd.read_csv('/workspace/data.csv')")
result = sbx.run_code("print(df.describe())")
print(result.stdout)

# Run shell commands
result = sbx.commands.run("pip install scikit-learn")
print(result.exit_code)

# File operations
sbx.files.write("/workspace/data.csv", b"col1,col2\n1,2\n3,4")
batch_result = sbx.files.write_batch(
    [
        ("/workspace/app.py", "print('hello')"),
        ("/workspace/data.bin", b"\x00\x01"),
    ]
)
assert batch_result.success
content = sbx.files.read("/workspace/output.txt")
files = sbx.files.list("/workspace")

# Pause / resume: both are asynchronous on the backend.
sbx.pause()  # blocks until the sandbox reports Paused
sbx.resume()  # returns immediately, phase is Resuming
sbx.wait_until_ready()  # block until the new pod is Running

# Cleanup. Deletion (including the persistence purge that releases the name)
# is asynchronous; pass wait=True when you need guaranteed reclamation.
sbx.kill(wait=True)
```

### Context Manager

```python
from prokube.sandbox import Sandbox

with Sandbox.from_pool("python-pool") as sbx:
    sbx.wait_until_ready()
    result = sbx.run_code("print(42)")
    print(result.stdout)
# Sandbox is automatically cleaned up
```

## Configuration

Configuration can be provided via environment variables or explicitly:

### Environment Variables

```bash
export PROKUBE_WORKSPACE=my-workspace
export PROKUBE_API_URL=https://prokube.ai/pkui  # Required for external access
export PROKUBE_TIMEOUT=300  # Optional, default 300 seconds
```

**Note:** In-cluster Agent Gateway access does not require SDK auth credentials.
`PROKUBE_API_KEY` enables external access and takes precedence over `PROKUBE_USER_ID`
or `KF_USER` when present.

### In-Cluster Notebooks

Inside a prokube.ai workspace notebook, only the workspace namespace is required.
If `PROKUBE_API_URL` is not set, the SDK defaults to the in-cluster Agent Gateway
service and routes sandbox traffic through `/_platform/sandbox/<workspace>`.

```bash
export PROKUBE_WORKSPACE=henrik
```

```python
from prokube.sandbox import Sandbox

with Sandbox.from_pool("python-pool") as sbx:
    sbx.wait_until_ready()
    result = sbx.run_code("print('Hello from inside the workspace!')")
    print(result.stdout)
```

This uses:
`http://agentgateway-proxy.agentgateway-system.svc.cluster.local/_platform/sandbox/henrik/sandboxes/claim`.

### Explicit Configuration

```python
from prokube.sandbox import Sandbox

sbx = Sandbox.from_pool(
    pool="python-pool",
    api_url="https://prokube.ai/pkui",
    workspace="my-workspace",
    user_id="user@example.com",
)
```

### External Access (API Key)

For accessing prokube from outside the cluster, use an API key:

```bash
export PROKUBE_API_URL=https://prokube.ai/pkui
export PROKUBE_WORKSPACE=my-workspace
export PROKUBE_API_KEY=your-api-key
```

```python
from prokube.sandbox import Sandbox

# API key is picked up from PROKUBE_API_KEY env var
with Sandbox.from_pool("python-pool") as sbx:
    sbx.wait_until_ready()
    result = sbx.run_code("print('Hello from outside the cluster!')")
    print(result.stdout)
```

Or pass the API key explicitly (no env vars needed):

```python
from prokube.sandbox import Sandbox

with Sandbox.from_pool(
    pool="python-pool",
    api_url="https://prokube.ai/pkui",
    workspace="my-workspace",
    api_key="your-api-key",
) as sbx:
    sbx.wait_until_ready()
    result = sbx.run_code("print('Hello from outside the cluster!')")
    print(result.stdout)
```

When using an API key, the SDK automatically routes requests to the external
API endpoints and skips the internal version compatibility check.

## API Reference

### Sandbox

The main class for interacting with sandboxes.

```python
class Sandbox:
    name: str  # Sandbox name
    workspace: str  # Workspace (Kubernetes namespace)
    status: str  # Pending, Running, Paused, Pausing, Resuming,
    # Deleting, Succeeded, Failed, Unknown

    @classmethod
    def from_pool(cls, pool: str, **config) -> Sandbox:
        """Claim sandbox from WarmPool (fast; poll with wait_until_ready)."""

    @classmethod
    def create(cls, image: str, **config) -> Sandbox:
        """Create sandbox directly (cold start)."""

    @classmethod
    def list_page(
        cls,
        *,
        limit: int = 25,
        continue_token: str | None = None,
        api_url: str | None = None,
        workspace: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> SandboxPage:
        """List one bounded page of sandboxes, across every phase."""

    def run_code(
        self, code: str, language: str = "python", timeout: int = 300
    ) -> CodeResult:
        """Execute code with stateful Jupyter kernel."""

    def pause(self, wait: bool = True, timeout: int = 300) -> None:
        """Pause the sandbox; blocks until it reports Paused by default."""

    def resume(self) -> None:
        """Request a resume; returns immediately with phase Resuming."""

    def wait_until_ready(self, timeout: int = 120) -> None:
        """Block until the sandbox phase is Running."""

    def kill(self, wait: bool = False, timeout: int = 300) -> None:
        """Destroy the sandbox; deletion completes asynchronously."""

    @property
    def commands(self) -> CommandRunner:
        """Access shell command runner."""

    @property
    def files(self) -> FileManager:
        """Access file operations."""
```

`list_page` returns one name-ordered listing across every sandbox phase. The
continuation token is an opaque keyset cursor: pass it back together with the
same `limit` to fetch the next page. Idle warm-pool capacity is internal
infrastructure and never appears in the listing.

Each sandbox on a page owns its own HTTP client. Use the page as a context
manager (or call `page.close()`) to release them without destroying the remote
sandboxes:

```python
with Sandbox.list_page(limit=10) as page:
    for sandbox in page.sandboxes:
        print(sandbox.name)
    next_token = page.continue_token if page.has_more else None

if next_token:
    with Sandbox.list_page(
        limit=10,
        continue_token=next_token,
    ) as page:
        ...
```

### CommandRunner

```python
class CommandRunner:
    def run(self, command: str, timeout: int = 300) -> CommandResult:
        """Execute shell command."""


class CommandResult(BaseModel):  # Pydantic model
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int

    @property
    def success(self) -> bool: ...
```

### FileManager

```python
class FileManager:
    def write(self, path: str, content: bytes | str) -> None:
        """Upload file to sandbox."""

    def write_batch(
        self, items: list[tuple[str, bytes | str]]
    ) -> BatchFileWriteResponse:
        """Best-effort batch upload with per-file results."""

    def read(self, path: str) -> bytes:
        """Download file from sandbox."""

    def list(self, path: str = "/workspace") -> list[FileInfo]:
        """List files in directory."""


class BatchFileWriteResponse(BaseModel):
    success: bool  # True only if every file write succeeded
    total: int  # Total requested file writes
    success_count: int  # Number of successful writes
    failure_count: int  # Number of failed writes
    results: list[BatchFileWriteResult]


class BatchFileWriteResult(BaseModel):
    index: int  # Original request position
    path: str  # Sandbox path for this entry
    success: bool  # Whether this file write succeeded
    error: str | None  # Failure detail for best-effort partial failures
```

### CodeResult

```python
class CodeResult(BaseModel):  # Pydantic model
    stdout: str
    stderr: str
    success: bool
    execution_time_ms: int
    error_name: str | None  # Set on failure
    error_value: str | None  # Set on failure
    traceback: list[str] | None  # Set on failure
    session_id: str | None  # For stateful execution
```

## Development

```bash
# Clone the repository
git clone https://github.com/prokube/prokube-sdk.git
cd prokube-sdk

# Install dependencies
uv sync --dev

# Run tests
uv run pytest

# Run linter
uv run ruff check .

# Format code
uv run ruff format .
```

## License

MIT
