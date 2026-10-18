import json
import time

import pytest
from ate_env import NotFoundError, ProcessInfo, ProcessOutput, ProcessState, RpcError
from click.testing import CliRunner

from prokube import auth
from prokube.cli import commands, ssh
from prokube.sandbox import TemplateError, templates
from prokube.sandbox._ateapi import ateapi_pb2 as ateapi
from tests.conftest import make_jwt
from tests.test_templates import IMAGE, PRESETS, SIZES

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
    template: tuple[str, str] = ("sandbox-templates", "coding-small-v1"),
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
        self.resized: dict[str, str] = {}

    def __call__(self, url: str, workspace: str) -> "FakeSandboxes":
        self.targets.append((url, workspace))
        return self

    async def __aenter__(self) -> "FakeSandboxes":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def create(self, name, *, preset, image, size, env, env_guest):
        self.calls.append(("create", name, preset, image, size, env, env_guest))
        if image:
            return actor("sbx-" + name, template=("team-a", f"{name}-v1"))
        return actor("sbx-" + name)

    async def resize(self, name, *, preset, size):
        self.calls.append(("resize", name, preset, size))
        if size not in (None, "small", "medium"):
            raise TemplateError(f"Unknown size {size!r}. Sizes: small, medium")
        if preset:
            return actor("sbx-" + name, "ACTOR_STATE_RUNNING")
        self.resized[name] = str(size)
        return actor(
            "sbx-" + name, "ACTOR_STATE_RUNNING", ("team-a", f"coding-small-v1-{size}")
        )

    async def has_env_guest(self, name):
        self.calls.append(("has_env_guest", name))
        return self.env_guest

    async def presets(self):
        return PRESETS

    async def sizes(self):
        return SIZES

    async def template(self, name):
        """A custom template, resized or small; "gone" has none."""
        if name == "gone":
            raise NotFoundError("actor template not found")
        size = SIZES.get(self.resized.get(name))
        return templates.build_template("team-a", f"{name}-v1", image=IMAGE, size=size)

    async def list(self):
        return [
            actor("sbx-one", "ACTOR_STATE_RUNNING"),
            actor("sbx-web", template=("team-a", "web-v1")),
            actor("sbx-gone", template=("team-a", "gone-v1")),
        ]

    async def suspend(self, name):
        self.calls.append(("suspend", name))
        return actor("sbx-" + name)

    async def resume(self, name):
        self.calls.append(("resume", name))
        return actor("sbx-" + name, "ACTOR_STATE_RUNNING")

    async def delete(self, name):
        self.calls.append(("delete", name))
        return actor("sbx-" + name, "ACTOR_STATE_DELETING")

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
    commands._save_config(URL, "team-a")
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
    saved = json.loads(commands._config_file().read_text())
    assert saved == {"url": URL, "workspace": "team-a"}


def test_login_keeps_the_workspace_only_for_the_same_platform(monkeypatch):
    monkeypatch.setattr(commands.auth, "login", lambda url, **kw: None)
    commands._save_config(URL, "team-a")

    pk("login", URL)
    assert commands._load_config() == {"url": URL, "workspace": "team-a"}

    pk("login", "https://other.example.com")
    assert commands._load_config() == {"url": "https://other.example.com"}


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

    commands._save_config(URL, None)
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

    commands._save_config(URL, None)
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
    result = pk("sandbox", "create", "demo")
    assert result.output == "Created demo (SUSPENDED, preset coding, size small)\n"

    result = pk("sandbox", "list")
    assert result.output.splitlines() == [
        "NAME  STATE      PRESET  SIZE   IMAGE",
        "one   RUNNING    coding  small",
        f"web   SUSPENDED  custom  small  {IMAGE}",
        "gone  SUSPENDED  custom  -      -",
    ]

    assert pk("sandbox", "suspend", "demo").output == "demo: SUSPENDED\n"
    assert pk("sandbox", "resume", "demo").output == "demo: RUNNING\n"
    assert pk("sandbox", "delete", "demo").output == "Deleted demo\n"
    assert sandboxes.calls == [
        ("create", "demo", None, None, None, {}, True),
        ("suspend", "demo"),
        ("resume", "demo"),
        ("delete", "demo"),
    ]


def test_create_from_a_preset(sandboxes, logged_in):
    result = pk("sandbox", "create", "demo", "--preset", "python")

    assert result.exit_code == 0, result.output
    assert sandboxes.calls == [("create", "demo", "python", None, None, {}, True)]


def test_create_from_a_custom_image_warns_about_env_and_the_env_guest(
    sandboxes, logged_in
):
    result = pk(
        "sandbox",
        "create",
        "web",
        "--image",
        "nginx:1.27",
        "--size",
        "medium",
        "--env",
        "MODE=dev",
        "--env",
        "URL=http://x?a=b",
        "--no-env-guest",
    )

    assert result.exit_code == 0, result.output
    env = {"MODE": "dev", "URL": "http://x?a=b"}
    assert sandboxes.calls == [
        ("create", "web", None, "nginx:1.27", "medium", env, False)
    ]
    assert result.output.splitlines() == [
        f"Created web (SUSPENDED, preset custom, size small, image {IMAGE})",
        "Its env is readable by every member of the workspace; pass secrets "
        "through credential injection.",
        "No env guest: SDK, terminal and SSH work only with images based on "
        "`pk-sandbox-coding`.",
    ]


def test_create_needs_key_value_env(sandboxes, logged_in):
    result = pk("sandbox", "create", "x", "--image", IMAGE, "--env", "NOVALUE")

    assert result.exit_code == 2
    assert "'NOVALUE' is not KEY=VALUE" in result.output
    assert sandboxes.calls == []


def test_create_help_says_custom_images_start_slower_and_names_the_base_image():
    help_text = " ".join(pk("sandbox", "create", "--help").output.split())

    assert "A custom image starts slower and has no network rules." in help_text
    assert (
        "SDK, terminal and SSH work only with images based on `pk-sandbox-coding`."
        in help_text
    )
    assert (
        "Private images work only from the platform's sandbox image registry, "
        "pinned by digest (NAME@sha256:...). If you need another registry, ask "
        "your platform admin to configure it." in help_text
    )
    assert "template" not in help_text.lower()


def test_resize_to_a_size_says_what_is_kept(sandboxes, logged_in):
    result = pk("sandbox", "resize", "demo", "--size", "medium")

    assert result.exit_code == 0, result.output
    assert sandboxes.calls == [("resize", "demo", None, "medium")]
    assert result.output.splitlines() == [
        "Resizing demo: /workspace is kept, running processes restart.",
        f"demo: RUNNING, preset custom, size medium, image {IMAGE}",
    ]


def test_resize_to_a_preset(sandboxes, logged_in):
    result = pk("sandbox", "resize", "demo", "--preset", "coding")

    assert result.exit_code == 0, result.output
    assert sandboxes.calls == [("resize", "demo", "coding", None)]
    assert result.output.splitlines()[-1] == "demo: RUNNING, preset coding, size small"


def test_resize_needs_a_preset_or_a_size(sandboxes, logged_in):
    for args in ((), ("--preset", "coding", "--size", "small")):
        result = pk("sandbox", "resize", "demo", *args)
        assert result.exit_code == 2
        assert "Pass either --preset or --size." in result.output
    assert sandboxes.calls == []


def test_size_errors_are_one_line_messages(sandboxes, logged_in):
    result = pk("sandbox", "resize", "demo", "--size", "huge")

    assert result.exit_code == 1
    assert result.output.splitlines()[-1] == (
        "Error: Unknown size 'huge'. Sizes: small, medium"
    )


def test_preset_list_marks_the_default(sandboxes, logged_in):
    result = pk("preset", "list")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "NAME          SIZE    DESCRIPTION",
        "coding *      small   Coding: opencode, SSH and a terminal",
        "coding-large  medium  Coding, large",
        "python        small   Python",
    ]


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
        "Created demo (SUSPENDED, preset coding, size small)"
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


def test_proxy_opens_the_ssh_websocket_with_the_login_token(logged_in, monkeypatch):
    opened = []
    monkeypatch.setattr(commands.ssh, "proxy", lambda *args: opened.append(args))

    result = pk("sandbox", "proxy", "demo")

    assert result.exit_code == 0, result.output
    path = "/svc/sandbox/v1alpha/atespaces/team-a/envs/sbx-demo/ports/8022/ssh"
    assert opened == [(f"wss://platform.example.com{path}", logged_in)]
