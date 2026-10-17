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
`SandboxClient` works in scripts and in notebooks.

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

`pk sandbox connect demo` installs your public key (`~/.ssh/id_ed25519.pub`,
or the key given with `--identity`) in the sandbox, pins the sandbox's host key
in `~/.config/prokube/ssh/known_hosts` and runs `ssh` as user `developer`. Both
keys go through the authenticated file API, so the host key is pinned before
the first connection. SSH travels over a WebSocket; `pk sandbox proxy` is the
`ProxyCommand` and sends your login token.

To use `ssh`, `scp`, `rsync` or an IDE such as VS Code Remote-SSH directly, run
`pk sandbox ssh-config demo` once and add this line at the top of
`~/.ssh/config`:

```
Include ~/.config/prokube/ssh/hosts/*.conf
```

Then `ssh pk.my-workspace.demo` works.

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

Every call sends `authorization: Bearer <Dex ID token>`; Agent Gateway passes
it through to Substrate, which authorizes the caller. By default the token
comes from the stored login. Any callable that returns a token can replace it:

```python
client = SandboxClient(url, "my-workspace", token_provider=lambda: my_token)
```

## Endpoints

All platform addresses are derived from the platform URL in
[`src/prokube/endpoints.py`](src/prokube/endpoints.py), including the SSH
WebSocket URL. The Agent Gateway routes are provisional until
prokube/prokube-neo#87 and #97 settle them.

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
