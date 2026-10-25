# prokube-sdk

Python SDK for the prokube.ai platform.

> Preview: sandboxes run on [Agent Substrate](https://github.com/agent-substrate/substrate).
> The SDK is a thin layer over the upstream
> [env client](https://github.com/agent-substrate/env/tree/main/clients/python)
> and the Substrate control-plane API (ate API).

## Installation

```bash
pip install git+https://github.com/prokube/prokube-sdk.git
# or
uv pip install git+https://github.com/prokube/prokube-sdk.git
```

The upstream env client is not on PyPI; it is installed from its Git
repository at a pinned commit, so `git` must be available.

## Login

```python
from prokube.auth import login, logout

login("https://my-platform.example.com")  # opens the browser
logout("https://my-platform.example.com")
```

`login` runs the OAuth Authorization Code flow with PKCE against the
platform's Dex (`https://<domain>/dex`, public client `prokube-cli`, redirect
to `http://localhost:8000/callback` or `:18000`). It prints the URL as well,
for when no browser opens.

Tokens are stored in the OS keyring. Without a usable keyring they go to
`~/.config/prokube/tokens/<domain>.json` with mode 0600 (`PROKUBE_CONFIG_DIR`
moves the directory). To skip the keyring, set
`PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`. The SDK refreshes the
ID token shortly before it expires and stores the rotated refresh token.

## Quick start

```python
from prokube.sandbox import SandboxClient

with SandboxClient("https://my-platform.example.com", "my-workspace") as client:
    client.create("demo")                        # from the default preset
    result = client.run("demo", "python3 -c 'print(6 * 7)'")
    print(result.exit_code, result.stdout)

    client.write_file("demo", "/workspace/hello.txt", "hello")
    print(client.read_file("demo", "/workspace/hello.txt"))
    client.write_files("demo", {"/workspace/src/a.py": "...", "/workspace/src/b.py": "..."})

    for actor in client.list():
        print(actor.metadata.name, actor.status.state)

    client.suspend("demo")
    client.resume("demo")
    client.delete("demo")

    print(client.presets())                      # the platform's presets
    client.create("big", preset="pk-default-large")
    client.resize("big", preset="pk-default-small")
```

`url` and `workspace` default to `PROKUBE_URL` and `PROKUBE_WORKSPACE`, then
to what `pk login` saved (see [Command line](#command-line)).
`SandboxClient` works in scripts and in notebooks. `read_file` and `write_file`
only accept paths under `/workspace`; use `run` for files elsewhere.
`write_files` sends many files as one gzipped tar archive, which `tar` unpacks
in the sandbox. That is much faster than one `write_file` per file, and it
also takes absolute paths outside `/workspace`.

### Async

```python
from prokube.sandbox import AsyncSandboxClient

async with AsyncSandboxClient("https://my-platform.example.com", "my-workspace") as client:
    await client.create("demo")
    print((await client.run("demo", "uname -a")).stdout)

    # The upstream env handle, e.g. for streaming output:
    process = await client.env("demo").start_process(["python3", "train.py"])
    async for output in process.output(follow=True):
        ...
```

## Command line

```bash
pip install 'prokube-sdk[cli] @ git+https://github.com/prokube/prokube-sdk.git'

pk login https://my-platform.example.com --workspace my-workspace
pk auth status

pk sandbox create demo --connect                    # ends in an SSH session
pk sandbox list
pk sandbox exec demo -- python3 -c 'print(6 * 7)'
pk sandbox cp train.py demo:/workspace/
pk sandbox cp demo:/workspace/model.pt .
pk sandbox suspend demo
pk sandbox resume demo
pk sandbox revert demo                              # after a crash: back to the last snapshot
pk sandbox delete demo
pk logout
```

`pk login` remembers the platform and the default workspace in
`~/.config/prokube/config.json`. `--url` and `--workspace` (or `PROKUBE_URL`
and `PROKUBE_WORKSPACE`) override them per command.

In a terminal, `pk --help` and `pk --version` show the prokube wordmark;
`PK_NO_BANNER=1` turns it off, `NO_COLOR` keeps it uncolored. Pipes and scripts
get plain output.

### SSH

Set up SSH once:

```bash
pk login https://my-platform.example.com --workspace my-workspace
pk sandbox create demo --install-ssh
```

Then `ssh pk.my-workspace.demo` works, and VS Code Remote-SSH and JetBrains
Gateway list `pk.my-workspace.demo` as a host, since they read `~/.ssh/config`.

ssh does not resume a suspended sandbox: `pk sandbox proxy` refuses it with
`Run: pk sandbox resume demo`, so an IDE that reconnects on its own does not
wake it. `pk sandbox connect` and `forward` resume it, like `exec`, `cp` and
the SDK. `pk sandbox ssh-config demo --resume` writes an entry that resumes it;
`connect`, `forward` and `create` keep the entry's setting.

`--install-ssh` (on `create` and `connect`, or `pk sandbox ssh-config demo
--install` for an existing sandbox) writes the sandbox's host entry to
`~/.config/prokube/ssh/hosts/pk.<workspace>.<name>.conf` and, once, adds this
line at the top of `~/.ssh/config` (created with mode 0600 if missing):

```
Include "/home/me/.config/prokube/ssh/hosts/*.conf"
```

`pk sandbox ssh-config --install` adds only that line. Without `--install`,
`pk sandbox ssh-config demo` prints the host entry instead.

`pk sandbox connect demo` installs your public key (`~/.ssh/id_ed25519.pub`,
or the key given with `--identity`) in the sandbox, pins the sandbox's host key
in `~/.config/prokube/ssh/known_hosts` and runs `ssh` as `root`. Both
keys go through the authenticated env guest API, so the host key is pinned
before the first connection. The host key is what
`/usr/local/bin/pk-sandbox-hostkey` prints in the sandbox; it gives each
sandbox its own key, also one cloned from a preset's golden snapshot
(prokube/sandbox-images#21). On older images without that command, the CLI
reads `/workspace/ssh/ssh_host_ed25519_key.pub`. SSH travels over a
WebSocket; `pk sandbox proxy` is the `ProxyCommand` and sends your login
token.

### Reach a server in a sandbox

For a server in the sandbox, such as `opencode serve`, Jupyter or a dev
server, forward a local port to it:

```bash
pk sandbox connect demo                            # there: OPENCODE_PASSWORD=<password> opencode serve --port 4096
pk sandbox forward demo 4096                       # another terminal; Ctrl-C stops it
OPENCODE_PASSWORD=<password> opencode --server http://localhost:4096   # a third terminal
```

`pk sandbox forward NAME REMOTE_PORT [--local-port PORT]` runs
`ssh -N -L localhost:PORT:127.0.0.1:REMOTE_PORT` over the same SSH path as
`connect`, with the same key setup and host-key pinning. The local port
listens on localhost only, and the local client needs no token. It stops with
a clear message when the local port is taken, when nothing listens on
`REMOTE_PORT` in the sandbox, or when the sandbox has no env guest.

A client that can send a header, such as Codex, can also call the server
directly through the platform, without a tunnel:

```
https://<domain>/svc/sandbox/v1alpha/atespaces/<workspace>/envs/sbx-<name>/ports/<port>/<path>
Authorization: Bearer <token>
```

The token is your Dex ID token (in Python:
`prokube.auth.LoginSession("https://<domain>").token()`), which expires and
must be refreshed, or a workspace pod's ServiceAccount token. The server
sees the request at `/<path>`. Only editors of the workspace get through.

### Presets

```bash
pk preset list                                     # the platform's presets, default marked *
pk sandbox create demo                             # from the default preset
pk sandbox create agent --preset pk-default-small-memory
pk sandbox resize demo --preset pk-default-medium
pk sandbox list                                    # NAME, STATE, PRESET, SIZE
```

The presets are the platform's (`GET https://<domain>/svc/sandbox/v1alpha/presets`,
prokube/prokube-neo#132); an unknown name lists them. A preset is a shared
template with a golden snapshot, so its sandboxes start fast. A new sandbox
gets the platform's default network rules (or its preset's own, if it has
them) as its Substrate `EgressPolicy`.

`pk sandbox resize` switches a sandbox to another preset with the same image
and snapshot fidelity: another size within the same group, for example
`pk-default-small` to `pk-default-medium`, but not to
`pk-default-medium-memory`. A running sandbox is
suspended first and resumed afterwards: `/workspace` is kept, running
processes restart. A suspended sandbox stays suspended. Network rules and
credentials stay. When the presets cannot be read, `pk sandbox list` shows
each sandbox's template instead and prints a warning.

SDK, terminal and SSH work only with images based on `pk-sandbox-coding`.
For a sandbox whose image is not, `exec`, `cp` and `connect` say that it has
no env guest.

### MCP server

`pk sandbox mcp` gives any MCP client the tools `list`, `create`, `run`,
`read_file`, `write_file`, `suspend`, `resume`, `revert` and `delete`. Each is one
`AsyncSandboxClient` call. Locally it speaks stdio and uses your `pk login`:

```bash
claude mcp add sandbox -- pk sandbox mcp
```

opencode, in `opencode.json`:

```json
{
  "mcp": {
    "sandbox": { "type": "local", "command": ["pk", "sandbox", "mcp"] }
  }
}
```

In a workspace, run the image
`europe-west3-docker.pkg.dev/prokube-internal/prokube-customer/prokube-cli`
as a ToolHive `MCPServer` with sandbox access. It then appears on the MCP
servers page and behind Agent Gateway like any other MCP server in the
workspace:

```yaml
apiVersion: toolhive.stacklok.dev/v1beta1
kind: MCPServer
metadata:
  name: sandbox
spec:
  image: europe-west3-docker.pkg.dev/prokube-internal/prokube-customer/prokube-cli:<tag>
  args: [sandbox, mcp, --port, "8080", --host, 0.0.0.0]
  transport: streamable-http
  mcpPort: 8080
  proxyPort: 8080
  serviceAccount: default-editor
  podTemplateSpec:
    metadata:
      labels:
        access-sandboxes: "true"   # the workload token, see Credentials
    spec:
      imagePullSecrets:
      - name: regcred-prokube
```

There every caller acts as the workspace (`default-editor`), and only on its
sandboxes. Who may call the server is decided by Agent Gateway, as for every
MCP server in the workspace.

## Network and credentials

A new sandbox gets its preset's network rules, or the ones passed as
`create(name, network=[...])`. `network` and `set_network` read and replace
them as the JSON of Substrate `EgressRule`s; changes apply right away. An
`https` rule can inject a credential at egress, so the sandbox never holds
it: the sandbox sends the header with any placeholder, and the egress
gateway puts in the value of a key from a Secret of the workspace with the
label `prokube.ai/sandbox-credential=true`:

```python
from prokube.sandbox import inject, passthrough

key = client.secret_uri("anthropic-api-key", "ANTHROPIC_API_KEY")
client.set_network("demo", [
    inject("api.anthropic.com", "x-api-key", key),
    passthrough("github.com", "registry.npmjs.org"),
])
```

`inject` and `passthrough` return the rules' JSON; `network` returns the same
form. The presets' images trust the gateway's CA (`SSL_CERT_FILE`,
`NODE_EXTRA_CA_CERTS` and others). Each host name and port can be in only one
rule.

## How it maps to Substrate

| SDK | Substrate |
|-----|-----------|
| workspace | Atespace with the same name |
| sandbox `demo` | Actor `sbx-demo` in that Atespace |
| `create`, `list`, `get`, `delete`, `suspend`, `resume`, `revert` | `ateapi.Control` (`CreateActor`, `ListActors`, ..., `RevertActor`) |
| `list_actors`, `workers`, `worker_actors` | `ListActors` (every Actor), `ListWorkers`, `ListWorkerActorAssignments`, all pages |
| preset | ActorTemplate `sandbox-templates/<template>` |
| `create` | `CreateActor` on the preset's template, then `CreateActorEgressPolicy` (preset's rules or `network`); if that fails, `DeleteActor` |
| `resize` | `SuspendActor`, `UpdateActor` (new `actor_template`), `ResumeActor` if it was running |
| `network`, `set_network` | `GetActorEgressPolicy`, `UpdateActorEgressPolicy` |
| `run`, `read_file`, `write_file`, `write_files`, `env()` | env guest API via `ate_env` |
| return values | `Actor` protos and `ate_env.ShellResult` |
| errors | `ate_env` errors (`NotFoundError`, `PermissionDeniedError`, `RpcError`, ...) |

A new sandbox is `SUSPENDED`; its first command or `resume()` starts it.
`delete` removes a sandbox in any state.

## Credentials

Every call sends `authorization: Bearer <token>`; Agent Gateway passes it
through to Substrate, which authorizes the caller. The credential comes from,
in this order:

1. `token_provider`, any callable that returns a token:
   `SandboxClient(url, "my-workspace", token_provider=lambda: my_token)`
2. an API key: `api_key`, else `PROKUBE_API_KEY`. It is sent as `x-api-key`
   instead of a token (see [API keys](#api-keys)).
3. the stored login for `url` (`prokube.auth.login`, `pk login`)
4. the pod's workload token: the file in `PROKUBE_SANDBOX_TOKEN_FILE`, else
   `/var/run/secrets/prokube.ai/sandbox/token`. The SDK reads it again
   shortly before the token in it expires, since Kubernetes replaces it.

### API keys

For CI, servers and agents outside the platform, create a key in the platform
UI under AI Gateway Keys, with the scope "All sandboxes" and the format
`x-api-key`. It can do what a workspace editor can, in the key's workspace
only. No `pk login` needed:

```sh
export PROKUBE_URL=https://my-platform.example.com
export PROKUBE_WORKSPACE=my-workspace
export PROKUBE_API_KEY=<key>
pk sandbox create demo
```

### In a workspace pod

In a notebook, pipeline step or agent pod of a workspace, the SDK needs no
configuration and no login when the pod has sandbox access (Kubeflow
PodDefault `access-sandboxes`; in the notebook spawner, "Allow access to
sandboxes" under Configurations). The pod then has a ServiceAccount token
for Substrate, and its identity is the workspace's ServiceAccount
`default-editor` (prokube/prokube-neo#100):

```python
from prokube.sandbox import SandboxClient

with SandboxClient() as client:   # workspace = the pod's namespace
    client.create("demo")
    print(client.run("demo", "id -un").stdout)
```

Without a platform URL the client calls Agent Gateway's in-cluster Service;
the workspace defaults to the pod's namespace. Pass `workspace` or set
`PROKUBE_WORKSPACE` to name another one; the pod's identity has access to its
own workspace only. Any JWT file that something keeps fresh works as the
workload token, for example a SPIRE JWT-SVID that spiffe-helper writes: point
`PROKUBE_SANDBOX_TOKEN_FILE` at it.

## Use from a server

A server that acts for many users, such as pkui, makes one client per
request with the caller's token. That is cheap: the client connects on its
first call, leaving `async with` closes it, and with a `token_provider` it
needs no login, keyring or workload token.

```python
from prokube.sandbox import AsyncSandboxClient

GATEWAY = "agentgateway-proxy.agentgateway-system.svc.cluster.local:80"

async def list_sandboxes(namespace: str, token: str):
    async with AsyncSandboxClient(
        workspace=namespace, endpoint=GATEWAY, token_provider=lambda: token
    ) as client:
        return await client.list()
```

`endpoint` is the `host:port` of Agent Gateway's in-cluster Service; ate-api,
the env guest API and the presets are all called there, and Substrate
authorizes each call with the caller's token. Without a workspace, only
`presets()`, `workers()`, `worker_actors(worker)` and
`list_actors(all_workspaces=True)` work; other calls raise `ValueError`.
Errors are the `ate_env` ones (`NotFoundError`, `PermissionDeniedError`, ...)
and `PresetError`.

Files can stream, so a large file never sits in memory whole:

```python
async for chunk in client.env("demo").read_file("/workspace/model.pt"):
    ...
await client.write_file("demo", "/workspace/data.csv", chunks)  # (async) iterable of bytes
```

## Endpoints

All platform addresses are derived from the platform URL in
[`src/prokube/endpoints.py`](src/prokube/endpoints.py): ate-api and the env
guest API over native gRPC at `<domain>:443`, and SSH over
`wss://<domain>/svc/sandbox/v1alpha/atespaces/<workspace>/envs/sbx-<name>/ports/8022/ssh`.
The Agent Gateway routes behind them are defined in prokube/prokube-neo#97.
Without a platform URL, or with `endpoint`, the same routes are reached
through Agent Gateway's in-cluster Service, by default
`agentgateway-proxy.agentgateway-system.svc.cluster.local:80` (plaintext
HTTP/2; the Istio sidecars encrypt the hop).

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
uv run pyright
```

The ate API stubs in `src/prokube/sandbox/_ateapi/` are generated from the
pinned Substrate proto. To move the pin, edit `scripts/gen-ateapi.sh` and run
it.

### Benchmark against E2B

`scripts/benchmark.py` runs the same operations on prokube sandboxes and on
[E2B](https://e2b.dev) and prints p50/p95 per operation: create + first
command, a command, two Python snippets in one interpreter, 1 KiB and 1 MiB
file write and read, suspend, resume until a command works, delete. It uses
your `pk login`; E2B is not an SDK dependency.

```bash
uv run --with e2b==2.56.0 --with e2b-code-interpreter==2.11.0 \
    scripts/benchmark.py --rounds 20 --e2b-key-file ~/.e2b-key \
    --location "Berlin, office" --json-output bench.json
```

Without `--e2b-key-file` only prokube is measured. `--preset` picks the
prokube preset; E2B pauses keep the memory only for presets with `MEMORY`
fidelity, and then a third snippet checks that the interpreter kept its state.
There is no Python kernel in a prokube sandbox: the script feeds a `python3`
process on stdin (`client.env(name).start_process(..., stdin=True)`), E2B uses
`run_code`.

`--sizes 1k,1m,100m` sets the file sizes (E2B files are also fetched from
their `download_url`), and `--files 10000` also writes that many 1 KiB files
of random content with one `write_files` call on both. A failed file
operation is listed under `errors` and the round goes on.
With `PROKUBE_API_KEY` set, prokube is called with that key instead of your
login; the JSON records which credential was used.

## License

MIT
