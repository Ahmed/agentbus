"""Delivery regressions, using temporary buses and no live agent sessions."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

import bus
import agentbus_identity as identity
import agentbus_messages as messaging
import agentbus_routing as routing
import session_hook


ROOT = pathlib.Path(__file__).resolve().parent


class TestDelivery(unittest.TestCase):
    """Verify inbox identity and bounded hook continuation."""

    def setUp(self):
        """Keep every test isolated from live bus state and other windows."""
        self.directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="agentbus-test-"))
        self.recipient = bus.connect(self.directory, session="recipient")
        self.sender = bus.connect(self.directory, session="sender")

    def hook(self, event="Stop", agent="codex", **fields):
        """Exercise the real hook entry point with controlled input and output.
        
        Args:
            event (str): Hook event being simulated.
            agent (str): CLI owning the simulated window.
            **fields (object): Hook payload overrides for the scenario.
        """
        payload = {"session_id": "recipient", "hook_event_name": event,
                   "turn_id": "turn", "cwd": str(ROOT)}
        payload.update(fields)
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["session_hook.py", "--agent", agent]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(bus, "BUS_DIR", self.directory), \
                mock.patch.dict(os.environ, {"AGENTBUS_WATCHER": "0"}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(session_hook.main(), 0)
        return json.loads(output.getvalue()) if output.getvalue() else None

    def _command(self, env, *args):
        """Check that separate shell processes share the same conversation.
        
        Args:
            env (dict): Environment passed to the shell process.
            *args (str): CLI arguments passed to bus.py.
        """
        return subprocess.run([sys.executable, str(ROOT / "bus.py"), *args],
                              env=env, cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_codex_name_survives_separate_shell_commands(self):
        """Verify codex name survives separate shell commands."""
        env = dict(os.environ, AGENTBUS_DIR=self.directory,
                   CODEX_THREAD_ID="recipient")
        env.pop("AGENTBUS_SESSION", None)


        name = self._command(env, "name", "codex-delivery")
        self.assertEqual(self._command(env, "name"), name)
        message_id = messaging.send_direct(self.sender, "claude", name, "routing probe")
        delivered = self.hook("PostToolUse")
        self.assertIsNotNone(delivered)
        self.assertIn("routing probe", delivered["hookSpecificOutput"]["additionalContext"])
        self.assertIn(message_id, bus.consumed_ids(self.recipient))
        self.assertEqual(self._command(env, "read", "codex"), "(no messages)")

    def test_claude_hook_shares_named_tool_inbox(self):
        # Model a dedicated Claude window: its tools know the process id,
        # while hook input supplies the conversation id.
        """Verify claude hook shares named tool inbox."""
        parents = {100: 200, 200: 1}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(os, "getpid", return_value=100), \
                mock.patch.object(identity, "_parent_of", side_effect=parents.get), \
                mock.patch.object(identity, "_process_name", return_value="claude"), \
                mock.patch.object(identity, "_process_start", return_value="1000"), \
                mock.patch.object(identity, "_is_shared_daemon", return_value=False):
            legacy = bus.connect(self.directory)
            bus.register(legacy, "claude")
            name = bus.set_name(legacy, "claude-delivery")
            bus.set_job(legacy, "delivery-debugging")
            messaging.send_direct(self.sender, "codex", name, "named Claude probe")
            delivered = self.hook("PostToolUse", agent="claude")
            self.assertIsNotNone(delivered)
            self.assertIn("named Claude probe", delivered["hookSpecificOutput"]["additionalContext"])
            tool_client = bus.connect(self.directory)
            self.assertEqual(tool_client.session, "recipient")
            self.assertEqual(bus.current_handle(tool_client, "claude"), name)
            self.assertEqual(tool_client.job, "delivery-debugging")
            self.assertEqual(bus.receive(tool_client, "claude"), [])

    def test_binding_preserves_unread_mail_and_read_fanout(self):
        """Verify binding preserves unread mail and read fanout."""
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(identity, "session_pid", return_value=200), \
                mock.patch.object(identity, "_process_name", return_value="claude"), \
                mock.patch.object(identity, "_process_start", return_value="1000"):
            legacy = bus.connect(self.directory, session="claude200")
            other = bus.connect(self.directory, session="other-window")
            bus.register(legacy, "claude")
            bus.register(other, "claude")
            name = bus.set_name(legacy, "claude-existing")
            seen = messaging.send_direct(self.sender, "codex", "claude", "already read", broadcast=True)
            self.assertEqual(bus.receive(legacy, "claude")[0]["id"], seen)
            pending = messaging.send_direct(self.sender, "codex", name, "still waiting")
            # The UUID hook skipped the named message before it knew the
            # name. Merging by the larger cursor would lose that message.
            self.assertEqual(bus.receive(self.recipient, "claude")[0]["id"], seen)
            bus.bind_session(self.recipient, "claude")
            delivered = bus.receive(self.recipient, "claude")
            self.assertEqual([m["id"] for m in delivered], [pending])
            self.assertEqual(bus.receive(other, "claude")[0]["id"], seen)

    def test_binding_does_not_redeliver_legacy_fanout(self):
        """Verify binding does not redeliver legacy fanout."""
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(identity, "session_pid", return_value=200), \
                mock.patch.object(identity, "_process_name", return_value="claude"), \
                mock.patch.object(identity, "_process_start", return_value="1000"):
            legacy = bus.connect(self.directory, session="claude200")
            other = bus.connect(self.directory, session="other-window")
            bus.register(legacy, "claude")
            bus.register(other, "claude")
            self.assertEqual(bus.receive(self.recipient, "claude"), [])
            seen = messaging.send_direct(self.sender, "codex", "claude", "already read", broadcast=True)
            self.assertEqual(bus.receive(legacy, "claude")[0]["id"], seen)
            bus.bind_session(self.recipient, "claude")
            self.assertEqual(bus.receive(self.recipient, "claude"), [])
            self.assertEqual(bus.receive(other, "claude")[0]["id"], seen)

    def test_binding_does_not_follow_reused_process_id(self):
        """Verify binding does not follow reused process id."""
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(identity, "session_pid", return_value=200), \
                mock.patch.object(identity, "_process_name", return_value="claude"), \
                mock.patch.object(identity, "_process_start", return_value="1000"):
            bus.bind_session(self.recipient, "claude")
            self.assertEqual(identity.bound_session(self.recipient.state,
                                               "claude200", 200), "recipient")
        with mock.patch.object(identity, "_process_start", return_value="2000"):
            self.assertIsNone(identity.bound_session(self.recipient.state,
                                                "claude200", 200))

    def test_codex_threads_keep_separate_names_and_inboxes(self):
        """Verify codex threads keep separate names and inboxes."""
        clients = []
        for thread in ("first-thread", "second-thread"):
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread}), \
                    mock.patch.dict(os.environ, {"AGENTBUS_SESSION": ""}):
                client = bus.connect(self.directory)
                bus.register(client, "codex")
                bus.set_name(client, "codex-task")
                clients.append(client)
        first, second = clients[0], clients[1]
        self.assertNotEqual(bus.current_handle(first, "codex"),
                            bus.current_handle(second, "codex"))
        target = bus.current_handle(first, "codex")
        direct = messaging.send_direct(self.sender, "claude", target, "one window")
        shared = messaging.send_direct(self.sender, "claude", "codex", "both windows", broadcast=True)
        self.assertEqual([m["id"] for m in bus.receive(second, "codex")], [shared])
        self.assertEqual([m["id"] for m in bus.receive(first, "codex")],
                         [direct, shared])

    def test_empty_stop_does_not_spend_continuation_budget(self):
        """Verify empty stop does not spend continuation budget."""
        self.assertIsNone(self.hook())
        self.assertFalse((pathlib.Path(self.recipient.state) / "wake.recipient").exists())

    def test_receipts_do_not_restart_a_turn(self):
        """Verify receipts do not restart a turn."""
        messaging.send_direct(self.sender, "claude", "codex", "receipt", kind="ack", broadcast=True)
        self.assertIsNone(self.hook())
        self.assertFalse((pathlib.Path(self.recipient.state) / "wake.recipient").exists())
        delivered = self.hook("PostToolUse")
        self.assertIsNotNone(delivered)

    def test_receipt_backlog_does_not_hide_actionable_mail(self):
        """Verify receipt backlog does not hide actionable mail."""
        for _ in range(session_hook.HOOK_MESSAGE_LIMIT + 1):
            messaging.send_direct(self.sender, "claude", "codex", "receipt", kind="ack", broadcast=True)
        messaging.send_direct(self.sender, "claude", "codex", "answer needed", broadcast=True)
        delivered = self.hook()
        self.assertEqual(delivered["decision"], "block")
        self.assertIn("answer needed", delivered["reason"])

    def test_only_actual_continuations_count_toward_cap(self):
        """Verify only actual continuations count toward cap."""
        for index in range(session_hook.MAX_CHAIN_CONTINUATIONS):
            self.assertIsNone(self.hook())
            messaging.send_direct(self.sender, "claude", "codex", f"probe {index}", broadcast=True)
            self.assertEqual(self.hook()["decision"], "block")
        message_id = messaging.send_direct(self.sender, "claude", "codex", "over budget", broadcast=True)
        self.assertIsNone(self.hook())
        self.assertEqual([m["id"] for m in bus.peek(self.recipient, "codex")],
                         [message_id])
        delivered = self.hook("UserPromptSubmit")
        self.assertIn("over budget", delivered["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((pathlib.Path(self.recipient.state) / "wake.recipient").exists())

    def test_named_roster_inbox_uses_recipient_handle(self):
        """Verify named roster inbox uses recipient handle."""
        bus.register(self.recipient, "codex")
        name = bus.set_name(self.recipient, "codex-recipient")
        message_id = messaging.send_direct(self.sender, "claude", name, "roster probe")
        waiting = bus.peek(self.sender, "codex", session="recipient")
        self.assertEqual([m["id"] for m in waiting], [message_id])
        self.assertEqual(bus.receive(self.recipient, "codex")[0]["id"], message_id)


if __name__ == "__main__":
    unittest.main()
