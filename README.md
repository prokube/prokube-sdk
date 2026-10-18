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
    client.create("demo")                        # the platform's default image
    result = client.run("demo", "python3 -c 'print(6 * 7)'")
    print(result.exit_code, result.stdout)

    client.write_file("demo", "/workspace/hello.txt", "hello")
    print(client.read_file("demo", "/workspace/hello.txt"))

    for actor in client.list():
        print(actor.metadata.name, actor.status.state)

    client.suspend("demo")
    client.resume("demo")
    client.delete("demo")

    print(client.sizes(), client.images())       # sizes and suggested images
    client.create("big", suggested="python", size="large", env={"MODE": "dev"})
    client.create("web", image="nginx:1.27", env_guest=False)
    client.update("big", size="small")
    print(client.settings("big"))
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

### Sandbox settings

A sandbox has an image, a size and environment variables:

```bash
pk sandbox create demo                                   # the platform's default suggested image
pk sandbox create py --suggested python --size medium --env MODE=dev
pk sandbox create web --image nginx:1.27 --no-env-guest
pk sandbox update demo --size large --env MODE=dev --env OLD_VAR
pk sandbox list                                          # NAME, STATE, SIZE, IMAGE
```

The sizes and suggested images are the platform's
(`GET https://<domain>/svc/sandbox/v1alpha/sizes` and `.../images`,
prokube/prokube-neo#127 and #132); an unknown name lists them. A suggested
image prefills the env and env guest setting and gives a new sandbox its
network rules (its Substrate `EgressPolicy`); a custom image gets none.
`--env KEY=VALUE` sets a variable and `--env KEY` removes one, for example a
prefilled one. Env values are readable by every workspace member; pass
secrets through credential injection.

Public images work by tag (resolved anonymously) or digest. Private images
work only from the platform's sandbox image registry, pinned by digest
(`NAME@sha256:...`); if you need another registry, ask your platform admin to
configure it. SDK, terminal and SSH work only with images based on
`pk-sandbox-coding`; for any other image, pass `--no-env-guest`. `exec`, `cp`
and `connect` then say that the sandbox has no env guest.

`pk sandbox update` changes the image, size or env. A running sandbox is
suspended first and resumed afterwards: `/workspace` is kept, running
processes restart. A suspended sandbox stays suspended. A size that matches
none of the platform's sizes shows as `custom`.

#### Naming rule

The settings live in a Substrate ActorTemplate in the workspace's Atespace,
which the SDK and pkui (prokube/pkui#4708) create when a combination is new
and reuse otherwise. Everything else in it is fixed
([`settings.py`](src/prokube/sandbox/settings.py)). Its name is `sbx-` plus
the first 12 hex characters of the SHA-256 of this UTF-8 JSON, with keys
sorted, no whitespace and non-ASCII characters unescaped:

```
{"env": <env, without the egress CA variables>, "envGuest": <bool>, "image": <image>, "size": <size name>}
```

The image is in canonical form: `<registry>/<repository>@sha256:<digest>`,
without a tag; Docker Hub images are `docker.io/library/<name>` or
`docker.io/<org>/<name>`. pkui and the SDK test the same vectors
(`CODING` is `europe-west3-docker.pkg.dev/prokube-internal/prokube-customer/pk-sandbox-coding@sha256:d4feeac8a1521095906c0c655730a156edeb8f63af0911b18bd7c956a71fbb43`,
`NGINX` is `docker.io/library/nginx@sha256:` and 64 zeros):

| ActorTemplate | env | envGuest | image | size |
|---|---|---|---|---|
| `sbx-22d9290a0255` | `{}` | `true` | `CODING` | `small` |
| `sbx-79efbaf0453d` | `{}` | `true` | `CODING` | `medium` |
| `sbx-5c4abe566352` | `{"A": "x y", "OPENCODE_DISABLE_AUTOUPDATE": "1"}` | `true` | `CODING` | `small` |
| `sbx-2044fb864129` | `{}` | `false` | `NGINX` | `small` |
| `sbx-bceccb4bca35` | `{"GREETING": "grüß dich"}` | `false` | `NGINX` | `small` |

## How it maps to Substrate

| SDK | Substrate |
|-----|-----------|
| workspace | Atespace with the same name |
| sandbox `demo` | Actor `sbx-demo` in that Atespace |
| `create`, `list`, `get`, `delete`, `suspend`, `resume` | `ateapi.Control` (`CreateActor`, `ListActors`, ...) |
| `run`, `read_file`, `write_file`, `env()` | env guest API via `ate_env` |
| sandbox settings | ActorTemplate `sbx-<hash>` in that Atespace (see the naming rule), created if missing |
| a suggested image's network rules | the Actor's EgressPolicy `default` |
| `update` | `SuspendActor`, `UpdateActor` (new `actor_template`), `ResumeActor` if it was running |
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
