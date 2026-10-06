import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_artifact_cli import cli, update

# Under the temp dir: realpath() on a made-up /home path is slow where /home is
# an automount (macOS).
_ROOT = os.path.realpath(tempfile.gettempdir())
UV_PREFIX = os.path.join(_ROOT, "u", ".local", "share", "uv", "tools", "x")
PIPX_PREFIX = os.path.join(_ROOT, "u", ".local", "pipx", "venvs", "x")
PIP_PREFIX = os.path.join(_ROOT, "u", "venv")
ENV_KEYS = ("CI", "CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "CLAUDE_ARTIFACT_NO_AUTO_UPDATE")


class UpdateCase(unittest.TestCase):
    """Checks enabled, state and log in a temp dir, nothing real fetched or run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_file = Path(tmp.name) / "update.json"
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        for patcher in (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(update, "STATE_FILE", str(self.state_file)),
            mock.patch.object(update, "LOG_FILE", str(Path(tmp.name) / "update.log")),
            mock.patch.object(update, "__version__", "1.0.0"),
            mock.patch.object(update.shutil, "which", return_value="/usr/bin/uv"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fetch = self.patch(update, "_fetch_latest", return_value="1.0.0")
        self.popen = self.patch(update.subprocess, "Popen")
        self.prefix("uv")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def prefix(self, kind):
        prefix = {"uv": UV_PREFIX, "pipx": PIPX_PREFIX, "pip": PIP_PREFIX}[kind]
        patcher = mock.patch.object(update.sys, "prefix", prefix)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def run_check(self, quiet=False):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            update.finish_check(update.start_check(), quiet=quiet)
        return err.getvalue()


class VersionTest(unittest.TestCase):
    def test_version_key(self):
        self.assertEqual(update.version_key("v1.2.3"), (1, 2, 3))
        self.assertEqual(update.version_key("1.2.3+local"), (1, 2, 3))
        for junk in ("", None, "1.2.0rc1", "x"):
            self.assertIsNone(update.version_key(junk))

    def test_is_newer(self):
        self.assertTrue(update.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(update.is_newer("0.2.0", "0.2.0"))
        self.assertFalse(update.is_newer("0.1.0", "0.2.0"))
        self.assertFalse(update.is_newer("0.3.0rc1", "0.2.0"))


class InstallKindTest(UpdateCase):
    def test_kinds_and_commands(self):
        for kind, argv in (
            ("uv", ["uv", "tool", "upgrade", "claude-artifact-cli"]),
            ("pipx", ["pipx", "upgrade", "claude-artifact-cli"]),
            ("pip", [update.sys.executable, "-m", "pip", "install", "-U", "claude-artifact-cli"]),
        ):
            with self.subTest(kind=kind):
                self.prefix(kind)
                self.assertEqual(update.install_kind(), kind)
                self.assertEqual(update.upgrade_argv(), argv)


class InstallMarkerTest(UpdateCase):
    def test_markers_win_over_paths(self):
        with tempfile.TemporaryDirectory() as env:
            with mock.patch.object(update.sys, "prefix", env):
                self.assertEqual(update.install_kind(), "pip")
                Path(env, "uv-receipt.toml").write_text("")
                self.assertEqual(update.install_kind(), "uv")
            with tempfile.TemporaryDirectory() as env2, mock.patch.object(update.sys, "prefix", env2):
                Path(env2, "pipx_metadata.json").write_text("{}")
                self.assertEqual(update.install_kind(), "pipx")


class CheckTest(UpdateCase):
    def test_off_in_ci_and_when_opted_out(self):
        for key in ("CI", "CLAUDE_ARTIFACT_NO_UPDATE_CHECK"):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: "1"}):
                self.assertIsNone(update.start_check())
        self.fetch.assert_not_called()

    def test_fetches_and_caches_for_a_day(self):
        self.assertEqual(self.run_check(), "")
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.state()["latest"], "1.0.0")
        self.run_check()
        self.assertEqual(self.fetch.call_count, 1)

    def test_stale_cache_fetches_again(self):
        self.state_file.write_text(json.dumps({"latest": "1.0.0", "checked_at": 0}))
        self.run_check()
        self.assertEqual(self.fetch.call_count, 1)

    def test_failed_first_check_still_backs_off(self):
        self.fetch.side_effect = OSError("offline")
        self.run_check()
        self.run_check()
        self.assertEqual(self.fetch.call_count, 1)

    def test_abandoned_fetch_counts_as_an_attempt(self):
        # The attempt is on disk before the thread runs, so a fetch killed at
        # exit still waits an hour before the next one.
        seen = {}
        self.fetch.side_effect = lambda: seen.setdefault("state", self.state()) and "1.0.0"
        self.run_check()
        age = time.time() - seen["state"]["checked_at"]
        self.assertAlmostEqual(age, update.CHECK_INTERVAL - update.FAIL_BACKOFF, delta=60)

    def test_corrupt_state_values_are_ignored(self):
        for bad in ("x", [], {"a": 1}, [1]):
            with self.subTest(bad=bad):
                self.state_file.write_text(
                    json.dumps(
                        {"checked_at": bad, "latest": bad, "upgrade_to": "1.1.0",
                         "upgrade_started_at": bad}
                    )
                )
                self.fetch.reset_mock()
                self.fetch.return_value = "1.1.0"
                self.run_check(quiet=True)
                self.fetch.assert_called_once()

    def test_fetch_failure_is_silent_and_backs_off_an_hour(self):
        self.fetch.side_effect = OSError("offline")
        self.assertEqual(self.run_check(), "")
        age = time.time() - self.state()["checked_at"]
        self.assertAlmostEqual(age, update.CHECK_INTERVAL - update.FAIL_BACKOFF, delta=60)
        self.popen.assert_not_called()


class AutoUpdateTest(UpdateCase):
    def test_uv_and_pipx_upgrade_in_the_background(self):
        for kind, tool in (("uv", "uv"), ("pipx", "pipx")):
            with self.subTest(kind=kind):
                self.state_file.unlink(missing_ok=True)
                self.popen.reset_mock()
                self.prefix(kind)
                self.fetch.return_value = "1.1.0"
                err = self.run_check()
                self.assertIn("1.0.0 → 1.1.0: updating in the background", err)
                argv = self.popen.call_args.args[0]
                self.assertEqual(argv[0], tool)
                kwargs = self.popen.call_args.kwargs
                self.assertIs(kwargs["stdin"], update.subprocess.DEVNULL)
                self.assertTrue(kwargs.get("start_new_session") or kwargs.get("creationflags"))
                self.assertEqual(self.state()["upgrade_to"], "1.1.0")

    def test_one_upgrade_per_release_per_day(self):
        self.fetch.return_value = "1.1.0"
        self.run_check()
        self.run_check()
        self.assertEqual(self.popen.call_count, 1)
        state = self.state()
        state["upgrade_started_at"] = 0
        self.state_file.write_text(json.dumps(state))
        self.run_check()
        self.assertEqual(self.popen.call_count, 2)

    def test_pip_install_gets_the_notice_only(self):
        self.prefix("pip")
        self.fetch.return_value = "1.1.0"
        err = self.run_check()
        self.popen.assert_not_called()
        self.assertIn("A new release of claude-artifact-cli is available: 1.0.0 → 1.1.0", err)
        self.assertIn("claude-artifact update", err)

    def test_opt_out_of_auto_update_gets_the_notice(self):
        self.fetch.return_value = "1.1.0"
        with mock.patch.dict(os.environ, {"CLAUDE_ARTIFACT_NO_AUTO_UPDATE": "1"}):
            err = self.run_check()
        self.popen.assert_not_called()
        self.assertIn("is available", err)

    def test_quiet_upgrades_silently(self):
        self.fetch.return_value = "1.1.0"
        self.assertEqual(self.run_check(quiet=True), "")
        self.popen.assert_called_once()

    def test_missing_tool_falls_back_to_the_notice(self):
        self.fetch.return_value = "1.1.0"
        with mock.patch.object(update.shutil, "which", return_value=None):
            self.assertIn("is available: 1.0.0 → 1.1.0", self.run_check())
        self.popen.assert_not_called()

    def test_attempt_already_made_today_falls_back_to_the_notice(self):
        self.fetch.return_value = "1.1.0"
        self.run_check()
        self.assertIn("is available: 1.0.0 → 1.1.0", self.run_check())
        self.popen.assert_called_once()

    def test_nothing_when_current(self):
        self.fetch.return_value = "1.0.0"
        self.assertEqual(self.run_check(), "")
        self.popen.assert_not_called()


class WindowsUpdateTest(UpdateCase):
    def test_update_runs_detached_on_windows(self):
        call = self.patch(update.subprocess, "call")
        with mock.patch.object(update.os, "name", "nt"), mock.patch.multiple(
            update.subprocess, DETACHED_PROCESS=8, CREATE_NEW_PROCESS_GROUP=512, create=True
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(update.cmd_update(None), 0)
        call.assert_not_called()
        self.assertEqual(self.popen.call_args.kwargs["creationflags"], 8 | 512)
        self.assertIn("upgrading in the background", err.getvalue())


class StateRaceTest(UpdateCase):
    def test_late_fetch_keeps_the_recorded_upgrade(self):
        self.fetch.return_value = "1.1.0"
        holder = update.start_check()
        holder["thread"].join()
        update._save_state({**update._load_state(), "upgrade_to": "1.1.0", "upgrade_started_at": 5})
        # A second fetch finishing late must merge into what's on disk.
        self.state_file.write_text(json.dumps({**self.state(), "checked_at": 0}))
        update.start_check()["thread"].join()
        self.assertEqual(self.state()["upgrade_to"], "1.1.0")


class CliTest(UpdateCase):
    def test_update_command_runs_the_upgrade(self):
        call = self.patch(update.subprocess, "call", return_value=0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["update"])
        self.assertEqual(code, 0)
        call.assert_called_once_with(["uv", "tool", "upgrade", "claude-artifact-cli"])
        self.assertIn("running: uv tool upgrade claude-artifact-cli", err.getvalue())
        self.fetch.assert_not_called()  # the command itself is the check

    def test_update_command_without_the_tool(self):
        with mock.patch.object(update.shutil, "which", return_value=None):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["update"]), 1)
        self.assertIn("uv is not on PATH", err.getvalue())

    def test_commands_check_and_never_change_their_outcome(self):
        self.fetch.return_value = "1.1.0"
        self.patch(cli.auth, "get_token", return_value="test-token")
        self.patch(cli.api.FrameClient, "_request", return_value=[])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["list"]), 0)
        self.assertEqual(out.getvalue(), "No artifacts.\n")
        self.assertIn("updating in the background", err.getvalue())
        self.popen.assert_called_once()

    def test_a_crashing_check_never_stops_the_command(self):
        self.patch(update, "start_check", side_effect=TypeError("boom"))
        self.patch(update, "finish_check", side_effect=TypeError("boom"))
        self.patch(cli.auth, "get_token", return_value="test-token")
        self.patch(cli.api.FrameClient, "_request", return_value=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["list"]), 0)
        self.assertEqual(out.getvalue(), "No artifacts.\n")

    def test_a_broken_state_dir_never_fails_the_command(self):
        self.fetch.return_value = "1.1.0"
        self.popen.side_effect = OSError("no")
        self.patch(cli.auth, "get_token", return_value="test-token")
        self.patch(cli.api.FrameClient, "_request", return_value=[])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["list"]), 0)

    def test_version_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().split()[0], "claude-artifact")
