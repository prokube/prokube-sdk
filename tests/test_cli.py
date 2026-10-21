import json
import socket
import time

import pytest
from ate_env import (
    NotFoundError,
    ProcessInfo,
    ProcessOutput,
    ProcessState,
    RpcError,
    ShellResult,
)
from click.testing import CliRunner

from prokube import auth
from prokube.cli import banner, commands, ssh
from prokube.sandbox import PresetError
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from tests.conftest import make_jwt
from tests.test_presets import PRESETS

URL = "https://platform.example.com"
HTTP2_ERROR = (
    "upstream call failed: SendRequest: http2 error: connection error detected: "
    "frame with invalid size"
)
NO_ENV_GUEST = (
    "Error: demo: this sandbox's image has no env guest (not based on "
    "pk-sandbox-coding), so exec, cp and SSH do not work in it.\n"
)


def actor(
    name: str,
    state: str = "ACTOR_STATE_SUSPENDED",
    template: tuple[str, str] = ("sandbox-templates", "pk-default-small-v1"),
) -> ateapi.Actor:
    return ateapi.Actor(
        metadata=ateapi.ResourceMetadata(atespace="team-a", name=name),
        actor_template=ateapi.ObjectRef(atespace=template[0], name=template[1]),
        status=ateapi.ActorStatus(state=state),
    )


class FakeProcess:
    def __init__(self, outputs: list[ProcessOutput]) -> None:
        self.outputs = outputs

    async def output(self, follow: bool = False):
        for output in self.outputs:
            yield output


class FakeEnv:
    def __init__(self, sandboxes: "FakeSandboxes", name: str) -> None:
        self.sandboxes = sandboxes
        self.name = name

    async def start_process(self, command):
        self.sandboxes.calls.append(("start_process", self.name, list(command)))
        if self.sandboxes.env_error:
            raise self.sandboxes.env_error
        exit_info = ProcessInfo(
            process_id="p1",
            command=tuple(command),
            pid=7,
            state=ProcessState.EXITED,
            exit_code=3,
            started_at=None,
            finished_at=None,
        )
        return FakeProcess(
            [
                ProcessOutput(stdout=b"out\n"),
                ProcessOutput(stderr=b"err\n"),
                ProcessOutput(exit=exit_info),
            ]
        )


class FakeSandboxes:
    """Stands in for AsyncSandboxClient; every client of a test shares it."""

    def __init__(self) -> None:
        self.targets: list[tuple[str, str]] = []
        self.calls: list[tuple] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.env_guest = True
        self.env_error: Exception | None = None
        self.presets_error: Exception | None = None
        # What pk-sandbox-hostkey prints; None is an older image without it.
        self.host_key: ShellResult | None = None
        self.proc_net_tcp = ""  # the sandbox's /proc/net/tcp and tcp6
        self.state = "ACTOR_STATE_RUNNING"  # what get returns

    def __call__(self, url: str, workspace: str) -> "FakeSandboxes":
        self.targets.append((url, workspace))
        return self

    async def __aenter__(self) -> "FakeSandboxes":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def create(self, name, *, preset):
        self.calls.append(("create", name, preset))
        return actor(
            "sbx-" + name, template=("sandbox-templates", "pk-default-large-v1")
        )

    async def resize(self, name, *, preset):
        self.calls.append(("resize", name, preset))
        if preset == "other":
            raise PresetError(
                "Preset 'other' has another image than sandbox 'demo'. Create a "
                "new sandbox from it instead."
            )
        return actor(
            "sbx-" + name,
            "ACTOR_STATE_RUNNING",
            ("sandbox-templates", "pk-default-medium-v1"),
        )

    async def presets(self):
        if self.presets_error:
            raise self.presets_error
        return PRESETS

    async def has_env_guest(self, name):
        self.calls.append(("has_env_guest", name))
        return self.env_guest

    async def list(self):
        return [
            actor("sbx-one", "ACTOR_STATE_RUNNING"),
            actor("sbx-two", template=("team-a", "mine-v1")),
        ]

    async def suspend(self, name):
        self.calls.append(("suspend", name))
        return actor("sbx-" + name)

    async def resume(self, name):
        self.calls.append(("resume", name))
        return actor("sbx-" + name, "ACTOR_STATE_RUNNING")

    async def get(self, name):
        return actor("sbx-" + name, self.state)

    async def revert(self, name):
        self.calls.append(("revert", name))
        return actor("sbx-" + name)

    async def delete(self, name):
        self.calls.append(("delete", name))
        return actor("sbx-" + name, "ACTOR_STATE_DELETING")

    async def run(self, name, command):
        self.calls.append(("run", name, command))
        if self.env_error:
            raise self.env_error
        if command == "cat /proc/net/tcp /proc/net/tcp6":
            return ShellResult(stdout=self.proc_net_tcp, stderr="", exit_code=0)
        if self.host_key is None:
            return ShellResult(stdout="", stderr="sh: not found\n", exit_code=127)
        return self.host_key

    async def read_file(self, name, path):
        if self.env_error:
            raise self.env_error
        if (name, path) not in self.files:
            raise NotFoundError(f"{path}: no such file")
        return self.files[(name, path)]

    async def write_file(self, name, path, data, *, mode=0o644):
        self.calls.append(("write_file", name, path, mode))
        self.files[(name, path)] = data.encode() if isinstance(data, str) else data
        return len(data)

    def env(self, name):
        return FakeEnv(self, name)


@pytest.fixture
def sandboxes(monkeypatch):
    fake = FakeSandboxes()
    monkeypatch.setattr(commands, "AsyncSandboxClient", fake)
    return fake


@pytest.fixture
def logged_in():
    """A saved platform and workspace with a valid stored login."""
    auth.save_config(URL, "team-a")
    token = make_jwt(time.time() + 3600)
    auth._save_tokens(
        URL, {"id_token": token, "refresh_token": "r", "expires_at": time.time() + 3600}
    )
    return token


def pk(*args: str):
    return CliRunner().invoke(commands.cli, list(args))


# --- Login and saved config ------------------------------------------------


def test_login_saves_the_platform_and_workspace(monkeypatch):
    logins = []
    monkeypatch.setattr(
        commands.auth, "login", lambda url, **kw: logins.append((url, kw))
    )

    result = pk("login", URL + "/", "--workspace", "team-a", "--no-browser")

    assert result.exit_code == 0, result.output
    assert logins == [(URL, {"open_browser": False})]
    saved = json.loads((auth.config_dir() / "config.json").read_text())
    assert saved == {"url": URL, "workspace": "team-a"}


def test_login_keeps_the_workspace_only_for_the_same_platform(monkeypatch):
    monkeypatch.setattr(commands.auth, "login", lambda url, **kw: None)
    auth.save_config(URL, "team-a")

    pk("login", URL)
    assert auth.load_config() == {"url": URL, "workspace": "team-a"}

    pk("login", "https://other.example.com")
    assert auth.load_config() == {"url": "https://other.example.com"}


def test_login_needs_an_https_url():
    result = pk("login", "http://platform.example.com")

    assert result.exit_code == 2
    assert "https://<domain>" in result.output


def test_auth_status_shows_platform_workspace_and_user(logged_in):
    result = pk("auth", "status")

    assert result.exit_code == 0, result.output
    assert f"Platform:  {URL}" in result.output
    assert "Workspace: team-a" in result.output
    assert "User:      x" in result.output  # sub, as the token has no email
    assert "valid until" in result.output


def test_auth_status_without_login():
    assert "pk login <url>" in pk("auth", "status").output

    auth.save_config(URL, None)
    result = pk("auth", "status")
    assert result.exit_code == 1
    assert f"Not logged in to {URL}. Run: pk login {URL}" in result.output


def test_logout_forgets_the_tokens(logged_in):
    result = pk("logout")

    assert result.exit_code == 0, result.output
    with pytest.raises(auth.LoginRequired):
        auth.LoginSession(URL)


# --- Target resolution -----------------------------------------------------


def test_sandbox_commands_need_a_platform_and_workspace(sandboxes):
    assert "No platform URL" in pk("sandbox", "list").output

    auth.save_config(URL, None)
    result = pk("sandbox", "list")
    assert result.exit_code == 2
    assert "No workspace" in result.output


def test_flags_and_environment_override_the_saved_target(
    sandboxes, logged_in, monkeypatch
):
    pk("sandbox", "list")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "team-b")
    pk("sandbox", "list")
    pk("sandbox", "list", "--url", "https://other.example.com", "-w", "team-c")

    assert sandboxes.targets == [
        (URL, "team-a"),
        (URL, "team-b"),
        ("https://other.example.com", "team-c"),
    ]


def test_sdk_errors_are_one_line_messages(sandboxes, logged_in, monkeypatch):
    def not_logged_in(url, workspace):
        raise auth.LoginRequired("no tokens")

    result = pk("sandbox", "cp", "demo:/nope", "x")
    assert result.exit_code == 1
    assert result.output == "Error: /nope: no such file\n"

    monkeypatch.setattr(commands, "AsyncSandboxClient", not_logged_in)
    result = pk("sandbox", "list")
    assert result.output == f"Error: Not logged in to {URL}. Run: pk login {URL}\n"


# --- Sandbox commands ------------------------------------------------------


def test_create_list_and_lifecycle(sandboxes, logged_in):
    result = pk("sandbox", "create", "demo", "--preset", "pk-default-large")
    assert result.output == (
        "Created demo (SUSPENDED, preset pk-default-large, size large)\n"
    )

    result = pk("sandbox", "list")
    assert result.output.splitlines() == [
        "NAME  STATE      PRESET            SIZE",
        "one   RUNNING    pk-default-small  small",
        "two   SUSPENDED  team-a/mine-v1    -",
    ]

    assert pk("sandbox", "suspend", "demo").output == "demo: SUSPENDED\n"
    assert pk("sandbox", "resume", "demo").output == "demo: RUNNING\n"
    assert pk("sandbox", "delete", "demo").output == "Deleted demo\n"
    assert sandboxes.calls == [
        ("create", "demo", "pk-default-large"),
        ("suspend", "demo"),
        ("resume", "demo"),
        ("delete", "demo"),
    ]


def test_revert_asks_before_discarding_changes(sandboxes, logged_in):
    declined = CliRunner().invoke(
        commands.cli, ["sandbox", "revert", "demo"], input="n\n"
    )
    assert declined.exit_code == 1
    assert sandboxes.calls == []

    confirmed = CliRunner().invoke(
        commands.cli, ["sandbox", "revert", "demo"], input="y\n"
    )
    assert confirmed.output.endswith("demo: SUSPENDED\n")
    assert pk("sandbox", "revert", "demo", "--yes").output == "demo: SUSPENDED\n"
    assert sandboxes.calls == [("revert", "demo"), ("revert", "demo")]


def test_revert_does_not_ask_for_a_crashed_sandbox(sandboxes, logged_in):
    sandboxes.state = "ACTOR_STATE_CRASHED"

    assert pk("sandbox", "revert", "demo").output == (
        "demo crashed; its changes since the last snapshot are lost.\ndemo: SUSPENDED\n"
    )
    assert sandboxes.calls == [("revert", "demo")]


def test_create_defaults_to_the_platforms_preset(sandboxes, logged_in):
    result = pk("sandbox", "create", "demo")

    assert result.exit_code == 0, result.output
    assert sandboxes.calls == [("create", "demo", None)]


def test_list_without_presets_shows_templates_and_one_warning(sandboxes, logged_in):
    sandboxes.presets_error = PresetError(
        "Cannot read the sandbox presets from https://x/presets: HTTP 404"
    )

    result = pk("sandbox", "list")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "NAME  STATE      PRESET                                 SIZE",
        "one   RUNNING    sandbox-templates/pk-default-small-v1  -",
        "two   SUSPENDED  team-a/mine-v1                         -",
    ]
    assert result.stderr == (
        "Warning: Cannot read the sandbox presets from https://x/presets: HTTP 404; "
        "showing templates instead.\n"
    )


def test_resize_switches_the_preset_and_says_what_is_kept(sandboxes, logged_in):
    result = pk("sandbox", "resize", "demo", "--preset", "pk-default-medium")

    assert result.exit_code == 0, result.output
    assert sandboxes.calls == [("resize", "demo", "pk-default-medium")]
    assert result.output.splitlines() == [
        "Resizing demo: /workspace is kept, running processes restart.",
        "demo: RUNNING, preset pk-default-medium, size medium",
    ]


def test_preset_errors_are_one_line_messages(sandboxes, logged_in):
    result = pk("sandbox", "resize", "demo", "--preset", "other")

    assert result.exit_code == 1
    assert result.output.splitlines()[-1] == (
        "Error: Preset 'other' has another image than sandbox 'demo'. Create a "
        "new sandbox from it instead."
    )


def test_preset_list_marks_the_default(sandboxes, logged_in):
    result = pk("preset", "list")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "NAME                      SIZE    DESCRIPTION",
        "pk-default-small *        small   Small: /workspace survives a suspend",
        "pk-default-medium         medium  Medium: /workspace survives a suspend",
        "pk-default-large          large   Large: /workspace survives a suspend",
        "pk-default-small-memory   small   Small, keeps memory: memory and running "
        "processes survive a suspend",
        "pk-default-medium-memory  medium  Medium, keeps memory: memory and running "
        "processes survive a suspend",
        "pk-default-large-memory   large   Large, keeps memory: memory and running "
        "processes survive a suspend",
    ]


def test_exec_without_env_guest_explains_why(sandboxes, logged_in):
    sandboxes.env_guest = False
    sandboxes.env_error = RpcError(HTTP2_ERROR)

    result = pk("sandbox", "exec", "demo", "--", "true")

    assert result.exit_code == 1
    assert result.output == NO_ENV_GUEST
    assert ("has_env_guest", "demo") in sandboxes.calls


def test_exec_errors_with_env_guest_stay_as_they_are(sandboxes, logged_in):
    sandboxes.env_error = RpcError("deadline exceeded")

    result = pk("sandbox", "exec", "demo", "--", "true")

    assert result.exit_code == 1
    assert result.output == "Error: deadline exceeded\n"


def test_cp_and_connect_without_env_guest_explain_why(
    sandboxes, logged_in, tmp_path, identity
):
    sandboxes.env_guest = False
    sandboxes.env_error = RpcError(HTTP2_ERROR)

    assert pk("sandbox", "cp", "demo:/workspace/a", str(tmp_path)).output == (
        NO_ENV_GUEST
    )
    assert pk("sandbox", "connect", "demo", "-i", str(identity)).output == (
        NO_ENV_GUEST
    )


def test_exec_streams_output_and_exits_with_the_command_code(sandboxes, logged_in):
    result = pk("sandbox", "exec", "demo", "--", "ls", "-l", "/workspace")

    assert result.exit_code == 3
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert sandboxes.calls == [("start_process", "demo", ["ls", "-l", "/workspace"])]


def test_cp_uploads_and_downloads(sandboxes, logged_in, tmp_path):
    local = tmp_path / "train.py"
    local.write_text("print(1)\n")
    local.chmod(0o755)

    assert pk("sandbox", "cp", str(local), "demo:/workspace/").exit_code == 0
    assert sandboxes.files[("demo", "/workspace/train.py")] == b"print(1)\n"
    assert sandboxes.calls == [("write_file", "demo", "/workspace/train.py", 0o755)]

    sandboxes.files[("demo", "/workspace/out.txt")] = b"result"
    assert pk("sandbox", "cp", "demo:/workspace/out.txt", str(tmp_path)).exit_code == 0
    assert (tmp_path / "out.txt").read_bytes() == b"result"


def test_cp_needs_exactly_one_sandbox_side(sandboxes, logged_in):
    for args in (("a", "b"), ("demo:/a", "demo:/b")):
        result = pk("sandbox", "cp", *args)
        assert result.exit_code == 2
        assert "Exactly one" in result.output


# --- SSH -------------------------------------------------------------------

HOST_KEY = "ssh-ed25519 AAAAhostkey root@sandbox\n"


@pytest.fixture
def identity(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    key.with_name("id_ed25519.pub").write_text("ssh-ed25519 AAAAuserkey me@laptop\n")
    return key


def test_connect_installs_the_key_pins_the_host_key_and_runs_ssh(
    sandboxes, logged_in, identity, monkeypatch
):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b"ssh-rsa AAAAother\n"
    execs = []
    monkeypatch.setattr(commands.os, "execvp", lambda file, args: execs.append(args))

    result = pk("sandbox", "connect", "demo", "-i", str(identity))
    pk("sandbox", "connect", "demo", "-i", str(identity))

    assert result.exit_code == 0, result.output
    # An older image: no pk-sandbox-hostkey, so the key file is pinned.
    assert ("run", "demo", ssh.HOST_KEY_COMMAND) in sandboxes.calls
    authorized = sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)]
    assert authorized == b"ssh-rsa AAAAother\nssh-ed25519 AAAAuserkey me@laptop\n"
    known_hosts = ssh.known_hosts_file().read_text()
    assert known_hosts == "pk.team-a.demo ssh-ed25519 AAAAhostkey\n"
    config_file = ssh.host_config_file("pk.team-a.demo")
    assert execs == [["ssh", "-F", str(config_file), "pk.team-a.demo"]] * 2
    config = config_file.read_text()
    assert "Host pk.team-a.demo\n" in config
    assert f"-m prokube.cli sandbox proxy demo --url {URL} --workspace team-a" in config
    assert f'IdentityFile "{identity}"' in config


def test_connect_pins_the_key_pk_sandbox_hostkey_prints(
    sandboxes, logged_in, identity, monkeypatch
):
    # A clone of a golden snapshot still has the golden's key in the file.
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""
    sandboxes.host_key = ShellResult(
        stdout="ssh-ed25519 AAAAownkey root@sbx-demo\n", stderr="", exit_code=0
    )
    monkeypatch.setattr(commands.os, "execvp", lambda file, args: None)

    result = pk("sandbox", "connect", "demo", "-i", str(identity))

    assert result.exit_code == 0, result.output
    known_hosts = ssh.known_hosts_file().read_text()
    assert known_hosts == "pk.team-a.demo ssh-ed25519 AAAAownkey\n"
    assert ("run", "demo", "/usr/local/bin/pk-sandbox-hostkey") in sandboxes.calls


def test_a_failing_pk_sandbox_hostkey_pins_nothing(sandboxes, logged_in, identity):
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""
    sandboxes.host_key = ShellResult(
        stdout="", stderr="cannot write key\n", exit_code=1
    )

    result = pk("sandbox", "ssh-config", "demo", "-i", str(identity))

    assert result.exit_code == 1
    assert result.output == (
        "Error: demo: /usr/local/bin/pk-sandbox-hostkey failed (exit 1): "
        "cannot write key\n"
    )
    assert not ssh.known_hosts_file().exists()


def test_ssh_config_prints_the_entry(sandboxes, logged_in, identity):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""

    result = pk("sandbox", "ssh-config", "demo", "-i", str(identity))

    assert result.exit_code == 0, result.output
    assert result.output == ssh.host_config_file("pk.team-a.demo").read_text()
    assert not ssh.user_config_file().exists()


def test_ssh_config_install_without_a_sandbox_only_adds_the_include(sandboxes):
    result = pk("sandbox", "ssh-config", "--install")

    assert result.exit_code == 0, result.output
    path = ssh.user_config_file()
    assert result.output == f'Added Include "{ssh.include_pattern()}" to {path}\n'
    assert path.read_text().startswith("# added by pk\n")
    assert sandboxes.targets == []  # needs no login

    result = pk("sandbox", "ssh-config", "--install")
    assert result.output == f"{path} already includes the pk ssh hosts\n"


def test_ssh_config_needs_a_sandbox_or_install():
    result = pk("sandbox", "ssh-config")

    assert result.exit_code == 2
    assert "Pass NAME, or --install." in result.output


def test_ssh_config_install_writes_the_entry_and_names_the_host(
    sandboxes, logged_in, identity
):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""

    result = pk("sandbox", "ssh-config", "demo", "--install", "-i", str(identity))

    assert result.exit_code == 0, result.output
    assert ssh.host_config_file("pk.team-a.demo").exists()
    assert ssh.include_pattern() in ssh.user_config_file().read_text()
    assert "SSH host: pk.team-a.demo (ssh pk.team-a.demo," in result.output


def test_create_install_ssh_writes_the_entry_and_the_include(
    sandboxes, logged_in, identity
):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""

    result = pk("sandbox", "create", "demo", "--install-ssh", "-i", str(identity))

    assert result.exit_code == 0, result.output
    assert "Host pk.team-a.demo\n" in ssh.host_config_file("pk.team-a.demo").read_text()
    assert ssh.include_pattern() in ssh.user_config_file().read_text()
    assert result.output.splitlines()[0] == (
        "Created demo (SUSPENDED, preset pk-default-large, size large)"
    )
    assert "SSH host: pk.team-a.demo" in result.output


def test_create_without_install_ssh_leaves_ssh_alone(sandboxes, logged_in):
    assert pk("sandbox", "create", "demo").exit_code == 0

    assert not ssh.host_config_file("pk.team-a.demo").exists()
    assert not ssh.user_config_file().exists()


def test_connect_install_ssh_adds_the_include_before_running_ssh(
    sandboxes, logged_in, identity, monkeypatch
):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""
    execs = []
    monkeypatch.setattr(commands.os, "execvp", lambda file, args: execs.append(args))

    result = pk("sandbox", "connect", "demo", "--install-ssh", "-i", str(identity))

    assert result.exit_code == 0, result.output
    config_file = ssh.host_config_file("pk.team-a.demo")
    assert config_file.exists()
    assert ssh.include_pattern() in ssh.user_config_file().read_text()
    assert "SSH host: pk.team-a.demo" in result.output
    assert execs == [["ssh", "-F", str(config_file), "pk.team-a.demo"]]


def test_connect_needs_an_ssh_key(sandboxes, logged_in, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    result = pk("sandbox", "connect", "demo")

    assert result.exit_code == 1
    assert "ssh-keygen" in result.output


def test_delete_forgets_the_ssh_entry(sandboxes, logged_in, identity, monkeypatch):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""
    pk("sandbox", "ssh-config", "demo", "-i", str(identity))

    pk("sandbox", "delete", "demo")

    assert ssh.known_hosts_file().read_text() == ""
    assert not ssh.host_config_file("pk.team-a.demo").exists()


def test_proxy_opens_the_ssh_websocket_with_the_login_token(
    sandboxes, logged_in, monkeypatch
):
    opened = []
    monkeypatch.setattr(commands.ssh, "proxy", lambda *args: opened.append(args))

    result = pk("sandbox", "proxy", "demo")

    assert result.exit_code == 0, result.output
    path = "/svc/sandbox/v1alpha/atespaces/team-a/envs/sbx-demo/ports/8022/ssh"
    credential = ("authorization", f"Bearer {logged_in}")
    assert opened == [(f"wss://platform.example.com{path}", credential)]


def test_proxy_sends_an_api_key_without_a_login(sandboxes, monkeypatch):
    opened = []
    monkeypatch.setattr(commands.ssh, "proxy", lambda *args: opened.append(args))
    monkeypatch.setenv("PROKUBE_URL", "https://platform.example.com")
    monkeypatch.setenv("PROKUBE_WORKSPACE", "team-a")
    monkeypatch.setenv("PROKUBE_API_KEY", "pk-key")

    result = pk("sandbox", "proxy", "demo")

    assert result.exit_code == 0, result.output
    assert opened[0][1] == ("x-api-key", "pk-key")


def test_proxy_does_not_resume_a_suspended_sandbox(sandboxes, logged_in, monkeypatch):
    opened = []
    monkeypatch.setattr(commands.ssh, "proxy", lambda *args: opened.append(args))
    sandboxes.state = "ACTOR_STATE_SUSPENDED"

    refused = pk("sandbox", "proxy", "demo")

    assert refused.exit_code == 1
    assert refused.output == (
        "Error: demo is suspended, and ssh does not resume it. "
        "Run: pk sandbox resume demo\n"
    )
    assert opened == []
    assert pk("sandbox", "proxy", "demo", "--resume").exit_code == 0
    assert len(opened) == 1


def test_ssh_config_resume_stays_until_ssh_config_drops_it(
    sandboxes, logged_in, identity, monkeypatch
):
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""
    monkeypatch.setattr(commands.os, "execvp", lambda file, args: None)
    proxy = f"sandbox proxy demo --url {URL} --workspace team-a"

    def entry() -> str:
        return ssh.host_config_file("pk.team-a.demo").read_text()

    pk("sandbox", "ssh-config", "demo", "--resume", "-i", str(identity))
    assert f"{proxy} --resume\n" in entry()
    pk("sandbox", "connect", "demo", "-i", str(identity))
    assert f"{proxy} --resume\n" in entry()
    pk("sandbox", "ssh-config", "demo", "-i", str(identity))
    assert f"{proxy}\n" in entry()


# --- Port forwarding -------------------------------------------------------

# Listening on port 80 (the env guest) and 4096 (0x1000), as /proc/net/tcp shows.
PROC_NET_TCP = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when\n"
    "   0: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000\n"
    "   1: 0100007F:1000 00000000:0000 0A 00000000:00000000 00:00000000\n"
)


@pytest.fixture
def forwardable(sandboxes, logged_in, monkeypatch):
    """Sandbox demo with a server on 4096; records ssh instead of running it."""
    sandboxes.files[("demo", ssh.HOST_KEY_PATH)] = HOST_KEY.encode()
    sandboxes.files[("demo", ssh.AUTHORIZED_KEYS_PATH)] = b""
    sandboxes.proc_net_tcp = PROC_NET_TCP
    execs: list[list[str]] = []
    monkeypatch.setattr(commands.os, "execvp", lambda file, args: execs.append(args))
    return execs


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_forward_runs_ssh_with_a_localhost_forward(forwardable, identity):
    local = free_port()

    result = pk(
        "sandbox",
        "forward",
        "demo",
        "4096",
        "--local-port",
        str(local),
        "-i",
        str(identity),
    )

    assert result.exit_code == 0, result.output
    assert result.output == (
        f"Forwarding http://localhost:{local} to port 4096 in demo. "
        "Press Ctrl-C to stop.\n"
    )
    config_file = ssh.host_config_file("pk.team-a.demo")
    assert forwardable == [
        [
            "ssh",
            "-F",
            str(config_file),
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"localhost:{local}:127.0.0.1:4096",
            "pk.team-a.demo",
        ]
    ]
    assert ssh.known_hosts_file().read_text() == (
        "pk.team-a.demo ssh-ed25519 AAAAhostkey\n"
    )


def test_forward_uses_the_remote_port_locally_by_default(
    forwardable, identity, monkeypatch
):
    monkeypatch.setattr(commands.ssh, "port_in_use", lambda port: False)

    result = pk("sandbox", "forward", "demo", "4096", "-i", str(identity))

    assert result.exit_code == 0, result.output
    assert "localhost:4096:127.0.0.1:4096" in forwardable[0]


def test_forward_refuses_a_busy_local_port(forwardable, sandboxes, identity):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        result = pk(
            "sandbox",
            "forward",
            "demo",
            "4096",
            "--local-port",
            str(port),
            "-i",
            str(identity),
        )

    assert result.exit_code == 1
    assert result.output == (
        f"Error: Local port {port} is in use. Pass another one with --local-port.\n"
    )
    assert sandboxes.calls == [] and forwardable == []


def test_forward_says_when_nothing_listens_in_the_sandbox(
    forwardable, sandboxes, identity
):
    result = pk(
        "sandbox",
        "forward",
        "demo",
        "8080",
        "--local-port",
        str(free_port()),
        "-i",
        str(identity),
    )

    assert result.exit_code == 1
    assert result.output == (
        "Error: Nothing listens on port 8080 in sandbox demo. Start the server "
        "there first.\n"
    )
    assert forwardable == []


def test_forward_without_env_guest_explains_why(forwardable, sandboxes, identity):
    sandboxes.env_guest = False
    sandboxes.env_error = RpcError(HTTP2_ERROR)

    result = pk(
        "sandbox",
        "forward",
        "demo",
        "4096",
        "--local-port",
        str(free_port()),
        "-i",
        str(identity),
    )

    assert result.output == NO_ENV_GUEST
    assert forwardable == []


@pytest.mark.parametrize("port", ["0", "65536", "http"])
def test_forward_needs_a_valid_port(sandboxes, logged_in, port):
    result = pk("sandbox", "forward", "demo", port)

    assert result.exit_code == 2
    assert sandboxes.calls == []


def test_forward_help_shows_the_opencode_example():
    help_text = pk("sandbox", "forward", "--help").output

    assert "pk sandbox forward demo 4096\n" in help_text
    assert "opencode --server http://localhost:4096\n" in help_text


def test_version_is_plain_outside_a_terminal():
    result = CliRunner().invoke(commands.cli, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"pk, version {banner.pk_version()}\n"


def test_help_starts_with_the_wordmark_in_a_terminal(monkeypatch):
    monkeypatch.setattr(banner, "wanted", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    result = CliRunner().invoke(commands.cli, ["--help"])
    assert result.output.startswith(banner.WORDMARK.splitlines()[0])
    assert f"v{banner.pk_version()}" in result.output
    assert "Command line for the prokube platform." in result.output


def test_subcommand_help_has_no_wordmark(monkeypatch):
    monkeypatch.setattr(banner, "wanted", lambda: True)
    result = CliRunner().invoke(commands.cli, ["sandbox", "--help"])
    assert banner.WORDMARK.splitlines()[2] not in result.output


def test_wordmark_ends_with_the_version(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    lines = banner.render().splitlines()
    assert lines[-1].endswith(f"v{banner.pk_version()}")
    assert len(lines[-1]) == max(len(line) for line in lines)


def test_no_wordmark_with_pk_no_banner(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("PK_NO_BANNER", "1")
    assert not banner.wanted()
