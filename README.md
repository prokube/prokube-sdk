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
    client.create("demo", template="python-v1")
    result = client.run("demo", "python3 -c 'print(6 * 7)'")
    print(result.exit_code, result.stdout)

    client.write_file("demo", "/workspace/hello.txt", "hello")
    print(client.read_file("demo", "/workspace/hello.txt"))

    for actor in client.list():
        print(actor.metadata.name, actor.status.state)

    client.suspend("demo")
    client.resume("demo")
    client.delete("demo")
```

`url` and `workspace` default to `PROKUBE_URL` and `PROKUBE_WORKSPACE`.
`SandboxClient` works in scripts and in notebooks. `read_file` and `write_file`
only accept paths under `/workspace`; use `run` for files elsewhere.

### Async

```python
from prokube.sandbox import AsyncSandboxClient

async with AsyncSandboxClient("https://my-platform.example.com", "my-workspace") as client:
    await client.create("demo", template="python-v1")
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

pk sandbox create demo --template coding --connect   # ends in an SSH session
pk sandbox list
pk sandbox exec demo -- python3 -c 'print(6 * 7)'
pk sandbox cp train.py demo:/workspace/
pk sandbox cp demo:/workspace/model.pt .
pk sandbox suspend demo
pk sandbox resume demo
pk sandbox delete demo
pk logout
```

`pk login` remembers the platform and the default workspace in
`~/.config/prokube/config.json`. `--url` and `--workspace` (or `PROKUBE_URL`
and `PROKUBE_WORKSPACE`) override them per command.

### SSH

Set up SSH once:

```bash
pk login https://my-platform.example.com --workspace my-workspace
pk sandbox create demo --install-ssh
```

Then `ssh pk.my-workspace.demo` works, and VS Code Remote-SSH and JetBrains
Gateway list `pk.my-workspace.demo` as a host, since they read `~/.ssh/config`.

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
in `~/.config/prokube/ssh/known_hosts` and runs `ssh` as user `developer`. Both
keys go through the authenticated file API, so the host key is pinned before
the first connection. SSH travels over a WebSocket; `pk sandbox proxy` is the
`ProxyCommand` and sends your login token.

## How it maps to Substrate

| SDK | Substrate |
|-----|-----------|
| workspace | Atespace with the same name |
| sandbox `demo` | Actor `sbx-demo` in that Atespace |
| `create`, `list`, `get`, `delete`, `suspend`, `resume` | `ateapi.Control` (`CreateActor`, `ListActors`, ...) |
| `run`, `read_file`, `write_file`, `env()` | env guest API via `ate_env` |
| return values | `Actor` protos and `ate_env.ShellResult` |
| errors | `ate_env` errors (`NotFoundError`, `PermissionDeniedError`, `RpcError`, ...) |

A new sandbox is `SUSPENDED`; its first command or `resume()` starts it.
`delete` removes a sandbox in any state. Templates come from the shared
template Atespace unless `template_atespace` names another one.

## Credentials

Every call sends `authorization: Bearer <token>`; Agent Gateway passes it
through to Substrate, which authorizes the caller. The token comes from, in
this order:

1. `token_provider`, any callable that returns a token:
   `SandboxClient(url, "my-workspace", token_provider=lambda: my_token)`
2. the stored login for `url` (`prokube.auth.login`, `pk login`)
3. the pod's workload token: the file in `PROKUBE_SANDBOX_TOKEN_FILE`, else
   `/var/run/secrets/prokube.ai/sandbox/token`. The SDK reads it again
   shortly before the token in it expires, since Kubernetes replaces it.

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
    client.create("demo", template="python-v1")
    print(client.run("demo", "id -un").stdout)
```

Without a platform URL the client calls Agent Gateway's in-cluster Service;
the workspace defaults to the pod's namespace. Pass `workspace` or set
`PROKUBE_WORKSPACE` to name another one; the pod's identity has access to its
own workspace only. Any JWT file that something keeps fresh works as the
workload token, for example a SPIRE JWT-SVID that spiffe-helper writes: point
`PROKUBE_SANDBOX_TOKEN_FILE` at it.

## Endpoints

All platform addresses are derived from the platform URL in
[`src/prokube/endpoints.py`](src/prokube/endpoints.py): ate-api and the env
guest API over native gRPC at `<domain>:443`, and SSH over
`wss://<domain>/svc/sandbox/v1alpha/atespaces/<workspace>/envs/sbx-<name>/ports/8022/ssh`.
The Agent Gateway routes behind them are defined in prokube/prokube-neo#97.
In a workspace pod without a platform URL, the same gRPC routes are reached
through `agentgateway-proxy.agentgateway-system.svc.cluster.local:80`
(plaintext HTTP/2; the Istio sidecars encrypt the hop).

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

## License

MIT
