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

## Configuration

Configuration can be provided via environment variables or explicitly:

### Environment Variables

```bash
export PROKUBE_API_URL=https://prokube.ai/pkui  # Can include path prefix
export PROKUBE_WORKSPACE=my-workspace
export PROKUBE_USER_ID=user@example.com  # Required (or KF_USER must be set)
export PROKUBE_TIMEOUT=300  # Optional, default 300 seconds
```

**Note:** `PROKUBE_USER_ID` is required for authentication. If not set, the SDK
will fall back to `KF_USER` (set by some Kubeflow deployments). If neither is
available, you must pass `user_id` explicitly when creating a Sandbox.

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
    
    def read(self, path: str) -> bytes:
        """Download file from sandbox."""
    
    def list(self, path: str = "/workspace") -> list[FileInfo]:
        """List files in directory."""
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
