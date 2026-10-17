# prokube-sdk

Python SDK for the prokube.ai platform.

## Personal Sandboxes: `pk` and Python (preview)

Install this feature branch in a virtual environment (Python 3.10+, Git):

```sh
pip install --upgrade 'git+https://github.com/prokube/prokube-sdk.git@58-sandbox-revert'
pk login https://hen-public-troodon.prokube.cloud
pk sandbox create my-project --connect
```

Login opens the existing platform SSO in your browser and also prints a fallback
link. Later, `pk login` reuses the saved platform and credential-store choice.
The platform must already have your personal Sandbox identity provisioned.
Detach from the tmux session with Ctrl-B, then D; reconnect with
`pk sandbox connect my-project`. Connecting requires stock OpenSSH (`ssh`).

```sh
pk sandbox list
pk sandbox exec my-project -- python3 -c 'print("hello")'
pk sandbox cp ./example.py my-project:/workspace/example.py
pk sandbox suspend my-project
pk sandbox resume my-project
pk sandbox ssh-config my-project
pk auth status
pk logout
```

CLI and Python share the same login and serialized refresh-token rotation:

```python
from prokube import SandboxClient

with SandboxClient() as client:
    sandbox = client.get("my-project")
    print(sandbox.run("printf hello").stdout)
    sandbox.write_file("example.txt", b"hello")
    print(sandbox.read_file("example.txt"))
```

Use `AsyncSandboxClient` with `async with` and await its operations for async code.
Closing a client or disconnecting leaves the Sandbox running; delete explicitly
with `pk sandbox delete my-project`.

Credentials use the system keyring by default. On systems without a supported
keyring, explicitly select `pk login URL --credential-store=file`; this stores
credentials in owner-readable (0600) files. Profiles and credentials live under
`~/.config/prokube` (`PROKUBE_CONFIG_DIR` overrides it). Use
`pk --profile NAME login URL` for another platform profile. Public profile JSON
contains no tokens. `pk logout` removes local credentials, not your SSO session.

`pk login URL --no-browser` prints the login link without launching a browser;
the browser must reach this machine's localhost callback (port 8000 or 18000).
An existing UI JWT can also be imported with `--token-file PATH` or
`--token-stdin`; imported tokens have no refresh token and need reimporting when
expired. Python never launches a browser implicitly.

This preview pins the upstream env Python client to a Git revision; it is not
yet a standalone PyPI release. Personal-Sandbox API-key authentication awaits
the platform validator. The existing SDK API below remains available.

Validation: 262 unit tests and live macOS acceptance against the Linux-hosted
platform cover real Dex/Keycloak PKCE login, concurrent refresh, CLI/SDK
lifecycle, file transfer, process stdin/output, suspend/resume and SSH/tmux.
Live acceptance uses explicit file storage; native keyring and other client
operating systems have not received equivalent live validation.

### Crash recovery

`connect` and `resume` do not automatically roll a crashed Sandbox back. If a
Sandbox is `CRASHED` and has a completed durable snapshot, explicitly recover it:

```sh
pk sandbox revert my-project
pk sandbox connect my-project
```

`revert` discards execution state newer than the saved snapshot and leaves the
Sandbox `SUSPENDED`; `connect` or `resume` then restores it. Running Sandboxes and
Sandboxes without a completed durable snapshot are rejected. Python exposes the
same `sandbox.revert()` method (await it on the async client).
Before shutting down the cluster, use `pk sandbox suspend NAME` and wait for
success to save a fresh snapshot. A node shutdown alone does not create one.

### Egress policies

Each Sandbox starts with deny-by-default egress. Select destinations from the
platform's approved catalog; listing the catalog does not grant access:

```sh
pk sandbox egress options
pk sandbox egress allow my-project pypi.org files.pythonhosted.org
pk sandbox egress get my-project
pk sandbox egress clear my-project
```

Rules currently cover exact DNS hosts on TCP port 443. `tls` passes TLS through;
`https` intercepts HTTPS for gateway credential injection. Raw TCP, arbitrary
ports and wildcard rules are not exposed by this client. A platform operator
can extend the catalog. Policies are stored/enforced by Substrate, not locally.
Updates propagate through native caches; existing TLS-passthrough connections
are not forcibly terminated when a rule is removed.

For a complete replacement, save this as `egress.json`:

```json
{"rules": [{"host": "pypi.org", "protocol": "tls"}, {"host": "files.pythonhosted.org", "protocol": "tls"}]}
```

Then run `pk sandbox egress apply my-project --file egress.json`.
`get` includes a `revision`; retaining it in the file detects changes since the
read. Without a revision, the client reads the current policy before replacing
it, still protecting against concurrent writes during that operation.

```python
from prokube import SandboxClient
from prokube.personal import EgressRule

with SandboxClient() as client:
    sandbox = client.get("my-project")
    sandbox.set_egress([EgressRule("pypi.org"), EgressRule("files.pythonhosted.org")])
    print(sandbox.get_egress())
```

### Secret injection

**Guest-visible files:** send bytes via stdin or a local file, without putting
the value in shell history or command arguments:

```sh
pk sandbox secret set my-project api-key --from-file ./private-token.txt
# Prints /workspace/home/.prokube-secrets/api-key, never the value.
pk sandbox secret remove my-project api-key
```

`--stdin` is an alternative to `--from-file`. Delivery is atomic, files are
0600 and the directory 0700, owned by the SSH `developer` user. Files are
guest-visible and persist across suspend/resume and snapshots. Removing a
file does not remove it from previous snapshots. Values may be binary, up to
1 MiB. No secret value is saved in the local CLI profile.

**Process environment:** use a private JSON file such as
`{"API_TOKEN": "your-token"}` and pass the option before the Sandbox name:

```sh
pk sandbox exec --env-file ./private-env.json my-project -- python3 app.py
```

The variables are supplied to that process and its children, not installed as
global shell settings. They are visible to guest code and may enter memory
snapshots. Programs can themselves print or log supplied secrets.

```python
import os
from prokube import SandboxClient

with SandboxClient() as client:
    sandbox = client.get("my-project")
    path = sandbox.write_secret("api-key", os.environ["API_TOKEN"].encode())
    result = sandbox.run("python3 app.py", env={"API_TOKEN": os.environ["API_TOKEN"]})
    sandbox.remove_secret("api-key")
```

The same methods are available on the async client; use `await`.

**Gateway injection:** the platform operator provisions the key in the existing
credential provider and assigns an alias to your identity and an exact host.
`pk sandbox egress options` lists your aliases and the header to send. For an
operator-provisioned alias `openai`, for example:

```sh
pk sandbox egress allow my-project api.openai.com --protocol https --credential openai
pk sandbox exec my-project -- curl -H 'Authorization: placeholder' https://api.openai.com/v1/models
```

The gateway replaces an existing placeholder header; absent headers are not
added. Policies contain aliases, never key values or arbitrary storage URIs.
The caller must trust the configured upstream: its responses can expose values
it received. The CLI does not upload gateway keys or grant provider access.

Gateway injection requires a newly created trust-enabled Sandbox. Existing
Sandboxes keep working with passthrough policies and file/env injection. New
templates project the rotating CA bundle at `/run/ate/egress-trust/ca.pem` and
set common CA variables for SDK/CLI-executed processes. In an SSH session or
with a custom TLS library, explicitly configure that bundle (e.g. curl
`--cacert /run/ate/egress-trust/ca.pem`). Production aliases are provisioned by
the operator; no demo credential is retained after acceptance tests.

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
