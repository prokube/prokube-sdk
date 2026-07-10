# prokube-sdk

Python SDK for the prokube.ai platform.

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

# Claim a sandbox from a warm pool (instant, <100ms)
sbx = Sandbox.from_pool("python-pool")

# Or create directly (cold start, ~10-30s)
sbx = Sandbox.create(image="pk-sandbox:python-datascience")

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
batch_result = sbx.files.write_batch([
    ("/workspace/app.py", "print('hello')"),
    ("/workspace/data.bin", b"\x00\x01"),
])
assert batch_result.success
content = sbx.files.read("/workspace/output.txt")
files = sbx.files.list("/workspace")

# Cleanup
sbx.kill()
```

### Context Manager

```python
from prokube.sandbox import Sandbox

with Sandbox.from_pool("python-pool") as sbx:
    result = sbx.run_code("print(42)")
    print(result.stdout)
# Sandbox is automatically cleaned up
```

### Sandboxes v2 (Firecracker)

`prokube.sandboxv2.SandboxV2` is a parallel client for Firecracker-backed microVM
sandboxes. It mirrors the v1 `Sandbox` surface (`run_code` / `commands` / `files` /
`pause` / `resume` / `kill`), adapted for v2: every sandbox runs on `fc-pod`
(the only runtime — there is no runtime choice), and sandboxes are addressed by
`namespace` (the v1 `workspace`). There is no warm pool; instead a **running**
sandbox can be snapshotted into a reusable FirecrackerSnapshot and a later
sandbox can resume-clone from it for a fast start.

```python
from prokube.sandboxv2 import SandboxV2

# Create a Firecracker microVM (cold start)
sbx = SandboxV2.create(
    image="pk-sandbox-base",
    resources={"vcpus": 2, "mem_mib": 2048},
    egress=False,                       # default: isolated (no outbound network)
    namespace="my-namespace",
)
sbx.wait_until_ready()                  # polls until phase == "Running"

# Stateful code, shell commands and files work exactly like v1
print(sbx.run_code("print(2 + 2)").stdout)
sbx.commands.run("pip install numpy")
sbx.files.write("/workspace/x.txt", "hello")
print(sbx.files.read("/workspace/x.txt").decode())

# Pause == native VM snapshot; resume == restore
sbx.pause()
sbx.resume()

sbx.kill()
```

Snapshots — capture a running sandbox into a reusable FirecrackerSnapshot, then
launch a new sandbox that resume-clones it (fast start instead of a cold boot).
Capture is asynchronous: `snapshot()` returns as soon as the backend accepts
the request, and the sandbox keeps running throughout. Poll
`SandboxV2.list_snapshots()` (or use `wait_for_snapshot_ready()`) for the
snapshot's `phase == "Ready"`:

```python
from prokube.sandboxv2 import SandboxV2

sbx = SandboxV2.create(image="pk-sandbox-base", namespace="my-namespace")
sbx.wait_until_ready()
sbx.commands.run("pip install numpy")   # bake state into the snapshot

snapshot_name = sbx.snapshot("my-warm-python")  # async: sandbox keeps running
SandboxV2.wait_for_snapshot_ready(snapshot_name, namespace="my-namespace")

clone = SandboxV2.from_snapshot(snapshot_name, namespace="my-namespace")
clone.wait_until_ready()
print(clone.run_code("import numpy; print(numpy.__version__)").stdout)
```

`SandboxV2` reuses the same `x-api-key` auth, HTTP client, and configuration as
v1, so the environment variables below apply unchanged.

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
    name: str           # Sandbox name
    workspace: str      # Workspace (Kubernetes namespace)
    status: str         # Pending, Running, Bound, Succeeded, Failed, Unknown
    
    @classmethod
    def from_pool(cls, pool: str, **config) -> Sandbox:
        """Claim sandbox from WarmPool (instant)."""
    
    @classmethod
    def create(cls, image: str, **config) -> Sandbox:
        """Create sandbox directly (cold start)."""
    
    def run_code(self, code: str, language: str = "python", timeout: int = 300) -> CodeResult:
        """Execute code with stateful Jupyter kernel."""
    
    def kill(self) -> None:
        """Destroy sandbox immediately."""
    
    @property
    def commands(self) -> CommandRunner:
        """Access shell command runner."""
    
    @property
    def files(self) -> FileManager:
        """Access file operations."""
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

    def write_batch(self, items: list[tuple[str, bytes | str]]) -> BatchFileWriteResponse:
        """Best-effort batch upload with per-file results."""

    def read(self, path: str) -> bytes:
        """Download file from sandbox."""

    def list(self, path: str = "/workspace") -> list[FileInfo]:
        """List files in directory."""


class BatchFileWriteResponse(BaseModel):
    success: bool           # True only if every file write succeeded
    total: int              # Total requested file writes
    success_count: int      # Number of successful writes
    failure_count: int      # Number of failed writes
    results: list[BatchFileWriteResult]


class BatchFileWriteResult(BaseModel):
    index: int              # Original request position
    path: str               # Sandbox path for this entry
    success: bool           # Whether this file write succeeded
    error: str | None       # Failure detail for best-effort partial failures
```

### CodeResult

```python
class CodeResult(BaseModel):  # Pydantic model
    stdout: str
    stderr: str
    success: bool
    execution_time_ms: int
    error_name: str | None      # Set on failure
    error_value: str | None     # Set on failure
    traceback: list[str] | None # Set on failure
    session_id: str | None      # For stateful execution
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
