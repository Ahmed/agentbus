"""Exercise persistent listeners against real file events and MCP pipes."""

import json as json_codec
import os as system
import pathlib as paths
import select as readiness
import subprocess as processes
import sys as runtime
import unittest as testing

import agentbus_events as events
import agentbus_messages as messaging
import bus as bus_api
import tests.agentbus_test_utils as fixtures
import tests.push_fixtures as push_fixtures


class TestKernelListener(fixtures.BusFixture):
    """Use real inotify watches to cover append and replacement delivery."""

    def setUp(self):
        """Keep every kernel event and message in an isolated /tmp bus."""
        super().setUp()
        self.client = self.window("events", "codex", "event-delivery")
        self.listener = events.Listener(self.client, "codex")
        self.addCleanup(self.listener.close)

    def test_idle_descriptor_is_not_readable(self):
        """No activity leaves the kernel wait blocked with no timer wake."""
        ready = readiness.select([self.listener.fileno()], [], [], 0.1)

        self.assertEqual(ready, ([], [], []))

    def test_append_between_subscription_and_wait_is_retained(self):
        """The kernel queues arrivals even before the waiter starts."""
        paths.Path(self.client.path).write_text("{}\n", encoding="utf-8")

        ready = readiness.select([self.listener.fileno()], [], [], 1)
        changed = self.listener.drain()

        self.assertEqual(ready[0], [self.listener.fileno()])
        self.assertTrue(changed)
        self.assertFalse(self.listener.drain())

    def test_atomic_compaction_keeps_subscription(self):
        """Replacing the log cannot detach a directory subscription."""
        replacement = paths.Path(self.directory, "replacement")
        replacement.write_text("{}\n", encoding="utf-8")
        replacement.replace(self.client.path)

        first = self.listener.drain()
        paths.Path(self.client.path).write_text("{}\n{}\n", encoding="utf-8")
        second = self.listener.drain()

        self.assertTrue(first)
        self.assertTrue(second)

    def test_only_own_presence_changes_release_deferred_mail(self):
        """Another window's heartbeat must not cause an inbox scan."""
        paths.Path(self.client.state, "presence.codex.elsewhere").write_text(
            "{}", encoding="utf-8")
        unrelated = self.listener.drain()
        paths.Path(self.client.state, "presence.codex.events").write_text(
            "{}", encoding="utf-8")
        own = self.listener.drain()

        self.assertFalse(unrelated)
        self.assertTrue(own)

    def test_owner_exit_wakes_without_a_poll(self):
        """A pidfd releases a quiet listener when its window closes."""
        owner = self.enterContext(
            push_fixtures.child_process(
                [
                    runtime.executable,
                    "-c",
                    "input()"],
                stdin=processes.PIPE,
                stdout=processes.DEVNULL,
                stderr=processes.DEVNULL))
        self.addCleanup(owner.wait, 3)
        self.addCleanup(owner.stdin.close)
        listener = events.Listener(self.client, "codex", pid=owner.pid)
        self.addCleanup(listener.close)
        owner.stdin.close()
        owner.stdin = None

        active = listener.wait(2)

        self.assertFalse(active)
        self.assertIsNotNone(owner.poll())


class ChannelFixture(fixtures.BusFixture):
    """Share the real MCP transport between enabled and passive launches."""

    channel_enabled = "1"

    def setUp(self):
        """Give the channel and sender separate identities on a private bus."""
        super().setUp()
        self.sender = self.window("sender", "codex", "push-delivery")
        self.target = self.window("target", "claude", "push-delivery")
        environment = dict(system.environ, AGENTBUS_DIR=self.directory,
                           AGENTBUS_SESSION=self.target.session,
                           AGENTBUS_CLAUDE_CHANNEL=self.channel_enabled,
                           AGENTBUS_REDIS="0")
        script = paths.Path(bus_api.__file__).with_name("agentbus_channel.py")
        self.process = self.enterContext(push_fixtures.child_process(
            [runtime.executable, str(script)], env=environment,
            stdin=processes.PIPE, stdout=processes.PIPE,
            stderr=processes.PIPE, bufsize=0))
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "clientInfo": {"name": "test", "version": "1"},
                               "capabilities": {}}})
        self.initialized = self._read()

    def _send(self, value):
        """Send JSON-RPC over the same stdin transport Claude uses.

        Args:
            value (dict): Protocol message.

        Returns:
            None: Complete newline-delimited message written.
        """
        self.process.stdin.write((json_codec.dumps(value) + "\n").encode())

    def _read(self):
        """Bound a broken integration without polling a model or a mailbox.

        Returns:
            dict: The next JSON-RPC frame from the actual MCP subprocess.
        """
        ready = readiness.select([self.process.stdout], [], [], 5)
        self.assertTrue(ready[0], "MCP process produced no response")
        line = self.process.stdout.readline()
        self.assertTrue(line, "MCP process exited before delivery")
        return json_codec.loads(line)

    def _start(self):
        """Complete the client handshake before expecting push events.

        Returns:
            None: The initialized notification is sent.
        """
        self._send({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})

    def _mail(self, text, kind="message"):
        """Append a real addressed envelope without touching the live bus.

        Args:
            text (str): Payload checked in the channel notification.
            kind (str): Envelope kind for actionable/receipt-only coverage.

        Returns:
            None: Envelope queued for the test window.
        """
        messaging.send_direct(self.sender, "codex",
                              bus_api.current_handle(self.target, "claude"),
                              text, kind=kind)


class TestClaudeChannel(ChannelFixture):
    """Drive the real MCP process without starting a paid model session."""

    def test_initial_backlog_is_pushed_without_read_tool(self):
        """Mail queued before initialization reaches Claude automatically."""
        self._mail("already waiting")

        self._start()
        notification = self._read()

        self.assertEqual(self.initialized["result"]["capabilities"],
                         {"experimental": {"claude/channel": {}}})
        self.assertEqual(notification["method"],
                         "notifications/claude/channel")
        self.assertIn("already waiting", notification["params"]["content"])
        self.assertEqual(bus_api.peek(self.target, "claude"), [])

    def test_same_listener_delivers_later_messages(self):
        """Multiple arrivals use one process with silence between events."""
        self._start()
        self._mail("first arrival")
        first = self._read()
        quiet = readiness.select([self.process.stdout], [], [], 0.2)

        self._mail("second arrival")
        second = self._read()

        self.assertIn("first arrival", first["params"]["content"])
        self.assertEqual(quiet, ([], [], []))
        self.assertIn("second arrival", second["params"]["content"])
        self.assertIsNone(self.process.poll())
        self.assertEqual(bus_api.peek(self.target, "claude"), [])

    def test_receipts_do_not_start_empty_turns(self):
        """Receipts must not make two listeners wake each other."""
        self._start()

        self._mail("receipt only", kind="ack")
        ready = readiness.select([self.process.stdout], [], [], 0.2)

        self.assertEqual(ready, ([], [], []))
        self.assertIsNone(self.process.poll())

    def test_project_check_reaches_an_idle_channel(self):
        """The project question arrives before any held content."""
        self._start()

        self._mail("confirm the project", kind="project_check")
        notification = self._read()

        self.assertIn("confirm the project",
                      notification["params"]["content"])

    def test_stdin_close_stops_the_listener(self):
        """Closing Claude's MCP connection also ends its persistent watcher."""
        self._start()

        self.process.stdin.close()
        self.process.stdin = None
        status = self.process.wait(timeout=5)

        self.assertEqual(status, 0)


class TestPassiveClaudeChannel(ChannelFixture):
    """Saved registrations must not consume mail in unwrapped windows."""

    channel_enabled = "0"

    def test_passive_server_keeps_mail_for_the_active_delivery_path(self):
        """A passive MCP connection answers pings without claiming mail."""
        self._mail("leave this for the session hook")

        self._start()
        self._send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        response = self._read()
        ready = readiness.select([self.process.stdout], [], [], 0.2)

        self.assertEqual(self.initialized["result"]["capabilities"], {})
        self.assertEqual(response, {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(ready, ([], [], []))
        self.assertIsNone(self.process.poll())
        self.assertEqual(len(bus_api.peek(self.target, "claude")), 1)


if __name__ == "__main__":
    testing.main()
