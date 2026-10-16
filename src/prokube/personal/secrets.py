"""Explicit guest-visible secret delivery; no local or server-side secret registry."""

import re
import shlex

from .errors import SandboxError

SECRET_DIRECTORY = "/workspace/home/.prokube-secrets"
MAX_SECRET_BYTES = 1024 * 1024

# Values travel over process stdin, never shell source/argv. The runtime's
# developer owns the directory and files, so SSH and SDK users can both use them.
# Directory-relative operations and O_NOFOLLOW avoid following guest symlinks.
_SCRIPT = r"""
import os, pwd, secrets, sys
user = pwd.getpwnam('developer')
if os.geteuid() == 0:
    os.setgroups([user.pw_gid])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
directory, name, action = sys.argv[1:]
try:
    os.mkdir(directory, 0o700)
except FileExistsError:
    pass
fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    if os.fstat(fd).st_uid != os.geteuid():
        raise RuntimeError('secret directory owner mismatch')
    os.fchmod(fd, 0o700)
    if action == 'remove':
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
    else:
        data = sys.stdin.buffer.read(1048577)
        if len(data) > 1048576:
            raise RuntimeError('secret too large')
        temporary = '.tmp-' + secrets.token_hex(16)
        try:
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
finally:
    os.close(fd)
"""


def secret_command(name: str, action: str) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise SandboxError(
            "Secret name: 1–64 letters, digits, dots, underscores or hyphens; start with a letter or digit."
        )
    return shlex.join(
        ["python3", "-c", _SCRIPT, SECRET_DIRECTORY, name, action]
    ), f"{SECRET_DIRECTORY}/{name}"


def check_environment(env):
    if env is not None and (
        not isinstance(env, dict)
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str)
            or "\0" in value
            for key, value in env.items()
        )
    ):
        raise SandboxError(
            "Environment must be a JSON object of variable names and string values without NUL bytes."
        )
    return env
