import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from prokube.personal import EgressRule
from prokube.personal.cli import parser
from prokube.personal.client import AsyncSandbox
from prokube.personal.errors import SandboxError
from prokube.personal.secrets import check_environment, secret_command


def test_atomic_secret_file_and_symlink_handling(tmp_path):
    if os.name != "posix":
        pytest.skip("guest helper requires POSIX")

    command, _ = secret_command("api-key", "write")
    args = shlex.split(command)
    # Run the actual guest helper using this test runner's account on the host.
    prelude = f"import pwd; current = pwd.getpwuid({os.getuid()}); pwd.getpwnam = lambda name: current; "
    directory = tmp_path / "secrets"
    value = b"fixture-secret\n\x00binary"

    def run(action, data=b""):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                prelude + args[2],
                str(directory),
                "api-key",
                action,
            ],
            input=data,
            capture_output=True,
        )

    result = run("write", value)
    assert result.returncode == 0, result.stderr
    path = directory / "api-key"
    assert path.read_bytes() == value
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    path.unlink()
    path.symlink_to(victim)
    assert run("write", b"rotated").returncode == 0
    assert victim.read_bytes() == b"untouched" and not path.is_symlink()
    assert path.read_bytes() == b"rotated"
    assert run("remove").returncode == 0 and not path.exists()
    assert run("remove").returncode == 0
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    assert run("write", b"blocked").returncode != 0
    assert not (tmp_path / "api-key").exists()


@pytest.mark.parametrize("name", ["../key", "/key", ".", "a/b", "a;echo", "", "x" * 65])
def test_invalid_secret_names(name):
    with pytest.raises(SandboxError):
        secret_command(name, "write")


@pytest.mark.parametrize("env", [["x"], {"A": 3}, {"A=B": "x"}, {"A": "x\0y"}])
def test_bad_environment_does_not_reveal_values(env):
    with pytest.raises(SandboxError):
        check_environment(env)


async def test_environment_and_secret_values_are_not_shell_arguments():
    env = SimpleNamespace(
        id="test", shell=AsyncMock(return_value=SimpleNamespace(exit_code=0))
    )
    client = SimpleNamespace(_ready=AsyncMock())
    sandbox = AsyncSandbox(client, env)
    value = "sensitive-'$value"
    await sandbox.run("application", env={"TOKEN": value})
    assert env.shell.call_args.args == ("application",)
    assert env.shell.call_args.kwargs["env"] == {"TOKEN": value}
    await sandbox.write_secret("token", value.encode())
    assert value not in env.shell.call_args.args[0]
    assert env.shell.call_args.kwargs["stdin"] == value.encode()


async def test_policy_preserves_explicit_revision():
    client = SimpleNamespace(
        request=AsyncMock(return_value={"revision": "uid/3", "rules": []})
    )
    sandbox = AsyncSandbox(client, SimpleNamespace(id="test"))
    await sandbox.set_egress([EgressRule("pypi.org")], revision="uid/2")
    assert client.request.call_count == 1
    sent = client.request.call_args.kwargs["json"]
    assert sent["revision"] == "uid/2" and sent["rules"][0]["host"] == "pypi.org"
    assert "credentialUri" not in json.dumps(sent)


def test_cli_env_input_before_command():
    args = parser().parse_args(
        ["sandbox", "exec", "--env-file", "private.json", "test", "--", "python3", "-V"]
    )
    assert str(args.env_file) == "private.json" and args.argv[-2:] == ["python3", "-V"]
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "sandbox",
                "secret",
                "set",
                "test",
                "token",
                "--stdin",
                "--from-file",
                "token.txt",
            ]
        )
