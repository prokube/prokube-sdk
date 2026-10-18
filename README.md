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

    for actor in client.list():
        print(actor.metadata.name, actor.status.state)

    client.suspend("demo")
    client.resume("demo")
    client.delete("demo")

    print(client.presets(), client.sizes())      # the platform's presets and sizes
    client.create("py", preset="python")
    client.create("mytool", image="ghcr.io/me/mytool:1.0", size="medium")
    client.resize("py", size="large")
```

`url` and `workspace` default to `PROKUBE_URL` and `PROKUBE_WORKSPACE`.
`SandboxClient` works in scripts and in notebooks. `read_file` and `write_file`
only accept paths under `/workspace`; use `run` for files elsewhere.

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

### Presets, custom images and sizes

```bash
pk preset list                                     # the platform's presets, default marked *
pk sandbox create demo                             # from the default preset
pk sandbox create py --preset python
pk sandbox create mytool --image ghcr.io/me/mytool:1.0 --size medium --env MODE=dev
pk sandbox resize demo --preset coding-large       # same image, another size
pk sandbox resize mytool --size small
pk sandbox list                                    # NAME, STATE, PRESET, SIZE, IMAGE
```

Presets and sizes are the platform's
(`GET https://<domain>/svc/sandbox/v1alpha/presets` and `.../sizes`,
prokube/prokube-neo#132 and #127); an unknown name lists them. A preset is a
shared template with a golden snapshot, so its sandboxes start fast; a new
sandbox gets the preset's network rules as its Substrate `EgressPolicy`.

A custom image gets its own template `<workspace>/NAME-v<n>`, one version
higher than the highest existing one; templates never change. It starts
slower than a preset and has no network rules. NAME must be a DNS label, must
not be taken by a preset and must leave room for `-v<n>-<size>` within 63
characters. Only the image, size and env can be chosen; storage location,
snapshot settings, sandbox runtime, capabilities, volumes (including
`/run/ate/actor` with the sandbox's name and uid) and worker pool are fixed
([`templates.py`](src/prokube/sandbox/templates.py)). Public images work by
tag (resolved anonymously) or digest. Private images work only from the
platform's sandbox image registry, pinned by digest (`NAME@sha256:...`); if
you need another registry, ask your platform admin to configure it. Env
values are readable by every workspace member; pass secrets through
credential injection. SDK, terminal and SSH work only with images based on
`pk-sandbox-coding`; for any other image, pass `--no-env-guest`. `exec`, `cp`
and `connect` then say that the sandbox has no env guest.

`pk sandbox resize --preset` switches to another preset with the same image.
`--size` switches to the size variant `<workspace>/<template>-<size>` of the
sandbox's template, created on first use without a golden snapshot (or back to
the template itself at its own size). A running sandbox is suspended first
and resumed afterwards: `/workspace` is kept, running processes restart. A
suspended sandbox stays suspended. Network rules and credentials stay. A size
that matches none of the platform's sizes shows as `custom`.

## How it maps to Substrate

| SDK | Substrate |
|-----|-----------|
| workspace | Atespace with the same name |
| sandbox `demo` | Actor `sbx-demo` in that Atespace |
| `create`, `list`, `get`, `delete`, `suspend`, `resume` | `ateapi.Control` (`CreateActor`, `ListActors`, ...) |
| `run`, `read_file`, `write_file`, `env()` | env guest API via `ate_env` |
| `create` from a preset | `CreateActor` on `sandbox-templates/<template>`, then `CreateActorEgressPolicy` |
| `create` from a custom image, size variants | `CreateActorTemplate` in the workspace's Atespace |
| `resize` | `SuspendActor`, `UpdateActor` (new `actor_template`), `ResumeActor` if it was running |
| return values | `Actor` protos and `ate_env.ShellResult` |
| errors | `ate_env` errors (`NotFoundError`, `PermissionDeniedError`, `RpcError`, ...) |

A new sandbox is `SUSPENDED`; its first command or `resume()` starts it.
`delete` removes a sandbox in any state.

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
    client.create("demo")
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
