"""Keep startup and explicit shell waits free of model-driven inbox checks."""

import json as json_codec
import os as system
import pathlib as paths
import select as readiness
import subprocess as processes
import sys as runtime
import unittest as testing

import agentbus_messages as messaging
import bus as bus_api
import tests.agentbus_test_utils as fixtures
import tests.push_fixtures as push_fixtures


class TestPushStartup(fixtures.BusFixture):
    """Run the real shell wrapper and hook against isolated session state."""

    def setUp(self):
        """Record CLI arguments without opening any real agent windows."""
        super().setUp()
        self.target = self.window("startup", "claude", "push-startup")
        self.sender = self.window("sender", "codex", "push-startup")
        self.root = paths.Path(bus_api.__file__).parent
        executable = paths.Path(self.directory, "claude")
        executable.write_text(
            f"#!{runtime.executable}\nimport json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], "
            "'channel': os.environ.get('AGENTBUS_CLAUDE_CHANNEL')}))\n")
        executable.chmod(0o700)
        self.environment = dict(
            system.environ,
            PATH=self.directory +
            system.pathsep +
            system.environ["PATH"],
            AGENTBUS_DIR=self.directory,
            AGENTBUS_SESSION=self.target.session,
            AGENTBUS_WATCHER="0",
            AGENTBUS_REDIS="0")

    def _launch(self, arguments):
        """Exercise argument forwarding in the installed shell function.

        Args:
            arguments (list[str]): Arguments of a user-issued Claude command.

        Returns:
            dict: Fake CLI's argv and native-channel environment flag.
        """
        result = processes.run(
            ["bash", "-c", 'source "$1"; shift; claude "$@"',
             "probe", str(self.root / "shell.sh"), *arguments],
            env=self.environment, capture_output=True, text=True, check=True)
        return json_codec.loads(result.stdout)

    def test_bare_start_loads_instructions_without_submitting_a_task(self):
        """Setup must leave Claude idle until actual work arrives."""
        launched = self._launch([])

        self.assertEqual(launched["channel"], "1")
        self.assertEqual(len(launched["argv"]), 6)
        self.assertEqual(launched["argv"][::2], [
            "--append-system-prompt", "--mcp-config",
            "--dangerously-load-development-channels"])
        self.assertEqual(launched["argv"][5], "server:agentbus-events")
        self.assertNotIn("$BUS read", launched["argv"][1])
        self.assertIn("Do not run", launched["argv"][1])
        self.assertIn("agentbus-events", json_codec.loads(launched["argv"][3])[
            "mcpServers"])

    def test_explicit_task_remains_the_user_prompt(self):
        """A real task must reach Claude without a substituted briefing."""
        launched = self._launch(["Explain this repository"])

        self.assertEqual(launched["argv"][0], "Explain this repository")
        self.assertEqual(len(launched["argv"]), 5)
        self.assertEqual(launched["channel"], "1")
        self.assertIn("server:agentbus-events", launched["argv"])

    def test_resume_also_enables_the_native_channel(self):
        """A resumed conversation must retain automatic mail delivery."""
        launched = self._launch(["--resume", "conversation"])

        self.assertEqual(launched["argv"][:2], ["--resume", "conversation"])
        self.assertEqual(launched["channel"], "1")
        self.assertIn("server:agentbus-events", launched["argv"])

    def test_noninteractive_commands_keep_their_original_arguments(self):
        """A print-mode script must not receive interactive channel flags."""
        launched = self._launch(["-p", "summarize this"])

        self.assertEqual(launched["argv"], ["-p", "summarize this"])
        self.assertIsNone(launched["channel"])

    def test_mcp_administration_is_unchanged(self):
        """Configuring MCP must not accidentally start the delivery server."""
        launched = self._launch(["mcp", "list"])

        self.assertEqual(launched["argv"], ["mcp", "list"])
        self.assertIsNone(launched["channel"])

    def test_native_hook_leaves_waiting_mail_for_the_channel(self):
        """A startup hook cannot steal mail before channel initialization."""
        messaging.send_direct(
            self.sender,
            "codex",
            bus_api.current_handle(
                self.target,
                "claude"),
            "belongs to the channel")
        payload = {"hook_event_name": "SessionStart",
                   "session_id": self.target.session, "cwd": self.directory}
        environment = dict(self.environment, AGENTBUS_CLAUDE_CHANNEL="1")

        result = processes.run(
            [runtime.executable, str(self.root / "session_hook.py"),
             "--agent", "claude"], env=environment,
            input=json_codec.dumps(payload),
            capture_output=True, text=True, check=True)

        self.assertEqual(result.stdout, "")
        self.assertEqual(len(bus_api.peek(self.target, "claude")), 1)


class TestManualWait(fixtures.BusFixture):
    """Explicit waits stay quiet until mail arrives."""

    def setUp(self):
        """Prepare an isolated shell wait with no live Redis notifications."""
        super().setUp()
        self.target = self.window("waiting", "claude", "event-wait")
        self.sender = self.window("sender", "codex", "event-wait")
        environment = dict(system.environ, AGENTBUS_DIR=self.directory,
                           AGENTBUS_SESSION=self.target.session,
                           AGENTBUS_REDIS="0")
        self.process = self.enterContext(push_fixtures.child_process(
            [runtime.executable, bus_api.__file__, "wait", "claude"],
            env=environment,
            stdout=processes.PIPE, stderr=processes.PIPE))

    def test_quiet_wait_exits_only_after_actionable_mail(self):
        """A quiet bus produces no output; actual mail completes the wait."""
        quiet = readiness.select([self.process.stdout], [], [], 0.2)
        running = self.process.poll()

        messaging.send_direct(
            self.sender,
            "codex",
            bus_api.current_handle(
                self.target,
                "claude"),
            "wake the explicit wait")
        output, error = self.process.communicate(timeout=5)

        self.assertEqual(quiet, ([], [], []))
        self.assertIsNone(running)
        self.assertEqual(self.process.returncode, 0)
        self.assertIn(b"1 waiting for claude", output)
        self.assertEqual(error, b"")
        self.assertEqual(len(bus_api.peek(self.target, "claude")), 1)


if __name__ == "__main__":
    testing.main()
