"""Cross-platform process helpers for the runtime.

These shim Unix-only stdlib behavior so the runtime works on Windows:
- os.getuid() does not exist on win32; fall back to the username.
- subprocess cannot spawn ".cmd" shims from a bare name on Windows.
- os.killpg/os.getpgid are Unix-only; fall back to terminate/kill.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from typing import Dict, List, Mapping, Optional, Tuple, Union


_WINDOWS_CMD_META = re.compile(r'([()\[\]%!^"`<>&|;,*? ])')
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def user_suffix() -> str:
    """Return a stable per-user identifier for temp/artifact paths.

    os.getuid() is Unix-only; fall back to the Windows username on win32.
    """
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return os.environ.get("USERNAME") or os.environ.get("USER") or "user"


def resolve_spawn_arg(cmd: List[str]) -> List[str]:
    """Resolve a bare command name to a spawnable executable.

    subprocess on Windows appends only ".exe" to bare names, so npm-style
    ".cmd" shims (opencode, copilot, ...) fail with FileNotFoundError.
    Resolving via shutil.which fixes this without requiring a shell.
    """
    if not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved:
        return [resolved, *cmd[1:]]
    return cmd


def _escape_windows_command(value: str) -> str:
    return _WINDOWS_CMD_META.sub(r"^\1", value)


def _escape_windows_argument(value: str, double_escape: bool) -> str:
    value = re.sub(r'(\\*)"', lambda match: match.group(1) * 2 + '\\"', value)
    value = re.sub(r"(\\*)$", lambda match: match.group(1) * 2, value)
    value = '"{}"'.format(value)
    value = _WINDOWS_CMD_META.sub(r"^\1", value)
    if double_escape:
        value = _WINDOWS_CMD_META.sub(r"^\1", value)
    return value


def _get_comspec(env: Mapping[str, str]) -> str:
    for key, value in env.items():
        if key.lower() == "comspec":
            return value
    return shutil.which("cmd.exe") or "cmd.exe"


def prepare_spawn(
    cmd: List[str],
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[Union[List[str], str], Dict[str, object]]:
    """Prepare a command and process-group options for ``subprocess``.

    Windows batch shims must run through cmd.exe. Arguments are escaped using
    the same rules as cross-spawn so cmd metacharacters stay literal instead of
    becoming a second command.
    """
    resolved = resolve_spawn_arg(cmd)
    if os.name != "nt":
        return resolved, {"start_new_session": True}

    options: Dict[str, object] = {"creationflags": _WINDOWS_NEW_PROCESS_GROUP}
    if not resolved or not resolved[0].lower().endswith((".cmd", ".bat")):
        return resolved, options

    command = resolved[0]
    normalized = command.replace("/", "\\").lower()
    double_escape = "\\node_modules\\.bin\\" in normalized
    shell_command = " ".join([
        _escape_windows_command(command),
        *[_escape_windows_argument(arg, double_escape) for arg in resolved[1:]],
    ])
    comspec = _get_comspec(env or os.environ)
    command_line = '{} /d /s /c "{}"'.format(
        subprocess.list2cmdline([comspec]),
        shell_command,
    )
    options["executable"] = comspec
    return command_line, options


def _taskkill(process, force: bool) -> None:
    command = ["taskkill", "/pid", str(process.pid), "/t"]
    if force:
        command.append("/f")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 and process.poll() is None:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "taskkill failed")


def terminate_process(process) -> None:
    """Terminate a subprocess, handling Unix-only process groups."""
    if hasattr(os, "killpg"):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    elif os.name == "nt":
        _taskkill(process, force=False)
    else:
        process.terminate()


def kill_process(process) -> None:
    """Force-kill a subprocess, handling Unix-only process groups."""
    if hasattr(os, "killpg"):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    elif os.name == "nt":
        _taskkill(process, force=True)
    else:
        process.kill()
