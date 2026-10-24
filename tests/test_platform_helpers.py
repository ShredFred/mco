from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class PlatformHelpersTests(unittest.TestCase):
    def test_user_suffix_uses_getuid_on_posix(self) -> None:
        with mock.patch.object(__import__("runtime.platform", fromlist=["os"]).os, "getuid", create=True, return_value=12345):
            from runtime.platform import user_suffix
            self.assertEqual(user_suffix(), "12345")

    def test_user_suffix_falls_back_to_username_on_win32(self) -> None:
        import runtime.platform as platform
        with mock.patch.object(platform, "os", spec=platform.os) as fake_os:
            delattr(fake_os, "getuid")
            fake_os.environ.get.side_effect = ["testuser", "testuser"]
            self.assertEqual(platform.user_suffix(), "testuser")

    def test_resolve_spawn_arg_resolves_bare_command(self) -> None:
        import runtime.platform as platform
        with mock.patch("runtime.platform.shutil.which", return_value="C:\\Tools\\npm.cmd"):
            self.assertEqual(
                platform.resolve_spawn_arg(["npm", "install"]),
                ["C:\\Tools\\npm.cmd", "install"],
            )

    def test_resolve_spawn_arg_preserves_argv_when_not_found(self) -> None:
        import runtime.platform as platform
        with mock.patch("runtime.platform.shutil.which", return_value=None):
            self.assertEqual(platform.resolve_spawn_arg(["npm", "install"]), ["npm", "install"])
            self.assertEqual(platform.resolve_spawn_arg([]), [])

    def test_prepare_spawn_uses_cmd_for_windows_batch_shims(self) -> None:
        import runtime.platform as platform
        env = {"COMSPEC": "C:\\Windows\\System32\\cmd.exe"}
        with mock.patch.object(platform.os, "name", "nt"), \
                mock.patch("runtime.platform.shutil.which", return_value="C:\\repo\\node_modules\\.bin\\agent.cmd"):
            args, options = platform.prepare_spawn(
                ["agent", "two words", 'a"b', "100%", "x&y"],
                env,
            )

        self.assertIsInstance(args, str)
        self.assertIn("/d /s /c", args)
        self.assertIn("agent.cmd", args)
        self.assertEqual(options["executable"], env["COMSPEC"])
        self.assertEqual(options["creationflags"], 0x00000200)
        self.assertNotIn("shell", options)

    def test_terminate_and_kill_use_killpg_on_posix(self) -> None:
        import runtime.platform as platform
        process = mock.Mock()
        process.pid = 42
        fake_signal = mock.Mock(SIGTERM=15, SIGKILL=9)
        with mock.patch.object(platform, "os", create=True) as fake_os, \
                mock.patch.object(platform, "signal", fake_signal):
            fake_os.getpgid.return_value = 99
            fake_os.killpg = mock.Mock()
            platform.terminate_process(process)
            fake_os.killpg.assert_called_once_with(99, 15)
            platform.kill_process(process)
            fake_os.killpg.assert_called_with(99, 9)
            self.assertFalse(process.terminate.called)
            self.assertFalse(process.kill.called)

    def test_terminate_and_kill_fall_back_to_process_controls(self) -> None:
        import runtime.platform as platform
        process = mock.Mock()
        with mock.patch.object(platform, "os", spec=platform.os) as fake_os:
            delattr(fake_os, "killpg")
            platform.terminate_process(process)
            process.terminate.assert_called_once_with()
            platform.kill_process(process)
            process.kill.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "Windows process semantics")
    def test_windows_batch_shim_preserves_argv_without_command_injection(self) -> None:
        from runtime.platform import prepare_spawn

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shim_dir = root / "node_modules" / ".bin"
            shim_dir.mkdir(parents=True)
            capture_script = root / "capture.py"
            capture_script.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            shim = shim_dir / "mco-argv-test.cmd"
            shim.write_text(
                '@echo off\r\n"%MCO_TEST_PYTHON%" "%MCO_TEST_CAPTURE%" %*\r\n',
                encoding="utf-8",
            )
            marker = root / "injected.txt"
            expected = [
                "two words",
                'quote"inside',
                "100% literal",
                "caret^value",
                "pipe|value",
                "ampersand&value",
                "& echo injected > {}".format(marker),
            ]
            env = os.environ.copy()
            env.update({
                "MCO_TEST_PYTHON": sys.executable,
                "MCO_TEST_CAPTURE": str(capture_script),
            })
            args, options = prepare_spawn([str(shim), *expected], env)

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                check=False,
                **options,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.strip()), expected)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows process semantics")
    def test_windows_force_kill_terminates_the_process_tree(self) -> None:
        from runtime.platform import kill_process, prepare_spawn

        with tempfile.TemporaryDirectory() as temp_dir:
            child_pid_path = Path(temp_dir) / "child.pid"
            parent_script = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            args, options = prepare_spawn(
                [sys.executable, "-c", parent_script, str(child_pid_path)],
                os.environ,
            )
            process = subprocess.Popen(args, **options)
            try:
                deadline = time.monotonic() + 10
                while not child_pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(child_pid_path.exists(), "parent did not record child pid")
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                kill_process(process)
                process.wait(timeout=10)

                deadline = time.monotonic() + 5
                while _windows_pid_exists(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(_windows_pid_exists(child_pid))
            finally:
                if process.poll() is None:
                    subprocess.run(
                        ["taskkill", "/pid", str(process.pid), "/t", "/f"],
                        capture_output=True,
                        check=False,
                    )


def _windows_pid_exists(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/fi", "PID eq {}".format(pid), "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in result.stdout


if __name__ == "__main__":
    unittest.main()
