"""Related-window routing regressions on isolated temporary buses."""

import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
import unittest.mock as mock

import agentbus_messages as messaging
import agentbus_routing as routing
import bus
import tests.agentbus_test_utils as test_utils


class RoutingFixture(unittest.TestCase):
    """Build temporary bus windows for routing regressions."""

    def setUp(self):
        """Keep every test isolated from live bus state and other windows."""
        self.directory = tempfile.mkdtemp(prefix="agentbus-routing-test-")
        self.addCleanup(shutil.rmtree, self.directory)
        environment = mock.patch.dict(os.environ, {"AGENTBUS_JOB": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.sender = self.window(
            "sender", "codex", "codex-routing", "routing")

    def window(self, session, agent, handle=None, job="routing"):
        """Create a registered window for routing scenarios.

        Args:
            session (str): Stable identity for the simulated window.
            agent (str): CLI owning the simulated window.
            handle (str or None): Optional published task handle.
            job (str): Declared work used by related-window routing.
        """
        client = bus.connect(self.directory, session=session,
                             cwd=os.path.join(self.directory, session))
        bus.set_job(client, job)
        bus.register(client, agent)
        if handle is not None:
            bus.set_name(client, handle)
        return client

    def age(self, client, agent, seconds):
        """Model idle windows without waiting for real heartbeat expiry.

        Args:
            client (Bus): Window whose presence or inbox is inspected.
            agent (str): CLI owning the simulated window.
            seconds (float): Age to assign to the heartbeat.
        """
        path = pathlib.Path(client.state) / \
            f"presence.{agent}.{client.session}"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["last_seen"] = time.time() - seconds
        path.write_text(json.dumps(record), encoding="utf-8")

    def send(self, to="claude", **kwargs):
        """Publish a routing probe from the common sender.

        Args:
            to (str): Requested destination address.
            **kwargs (object): Additional send options for this scenario.
        """
        return messaging.send_direct(
            self.sender, "codex", to, "routing probe", **kwargs)

    def ids(self, client, agent):
        """Inspect delivery without depending on message bodies.

        Args:
            client (Bus): Window whose presence or inbox is inspected.
            agent (str): CLI owning the simulated window.
        """
        return [message["id"] for message in bus.receive(client, agent)]

    def assert_rejected_without_append(self, to="claude", **kwargs):
        """Verify unresolved addresses cannot leave stray envelopes.

        Args:
            to (str): Requested destination address.
            **kwargs (object): Additional send options for this scenario.
        """
        before = pathlib.Path(self.sender.path).read_bytes()
        with self.assertRaises(ValueError):
            self.send(to, **kwargs)
        self.assertEqual(pathlib.Path(self.sender.path).read_bytes(), before)


class TestRelatedRouting(RoutingFixture):
    """Verify automatic routing chooses one related window."""

    def test_task_match_beats_unrelated_same_job_and_ignores_number(self):
        """Verify task match beats unrelated same job and ignores number."""
        target = self.window(
            "related",
            "claude",
            "claude-routing-007",
            "other")
        unrelated = self.window(
            "unrelated",
            "claude",
            "claude-other",
            "routing")
        record = self.send(return_record=True)
        self.assertEqual(record["to"], bus.current_handle(target, "claude"))
        self.assertEqual(record["to_session"], target.session)
        self.assertEqual(record["from_session"], self.sender.session)
        self.assertEqual(record["requested_to"], "claude")
        self.assertEqual(self.ids(unrelated, "claude"), [])
        self.assertEqual(self.ids(target, "claude"), [record["id"]])

    def test_exact_job_breaks_tie_between_matching_tasks(self):
        """Verify exact job breaks tie between matching tasks."""
        other_job = self.window(
            "other-job",
            "claude",
            "claude-routing",
            "other")
        target = self.window("related", "claude", "claude-routing", "routing")
        message_id = self.send()
        self.assertEqual(self.ids(other_job, "claude"), [])
        self.assertEqual(self.ids(target, "claude"), [message_id])

    def test_matching_task_and_job_duplicates_are_ambiguous(self):
        """Verify matching task and job duplicates are ambiguous."""
        self.window("first", "claude", "claude-routing")
        self.window("second", "claude", "claude-routing")
        self.assert_rejected_without_append()

    def test_ambiguous_task_does_not_fall_back_to_unrelated_job_match(self):
        """Verify ambiguous task does not fall back to unrelated job match."""
        self.window("first", "claude", "claude-routing", "first-job")
        self.window("second", "claude", "claude-routing", "second-job")
        self.window("unrelated", "claude", "claude-other", "routing")
        self.assert_rejected_without_append()

    def test_unique_exact_job_matches_when_tasks_differ(self):
        """Verify unique exact job matches when tasks differ."""
        target = self.window(
            "related",
            "claude",
            "claude-different",
            "routing")
        unrelated = self.window("unrelated", "claude", "claude-other", "other")
        message_id = self.send()
        self.assertIsInstance(message_id, str)
        self.assertEqual(self.ids(unrelated, "claude"), [])
        self.assertEqual(self.ids(target, "claude"), [message_id])

    def test_multiple_job_matches_without_task_match_are_ambiguous(self):
        """Verify multiple job matches without task match are ambiguous."""
        self.window("first", "claude", "claude-first")
        self.window("second", "claude", "claude-second")
        self.assert_rejected_without_append()

    def test_only_unrelated_window_is_not_selected(self):
        """Verify only unrelated window is not selected."""
        self.window("unrelated", "claude", "claude-other", "other")
        self.assert_rejected_without_append()

    def test_missing_cli_recipient_is_not_queued_as_fanout(self):
        """Verify missing cli recipient is not queued as fanout."""
        self.assert_rejected_without_append()

    def test_idle_related_window_beats_active_unrelated_window(self):
        """Verify idle related window beats active unrelated window."""
        target = self.window("idle", "claude", "claude-routing", "other")
        self.age(target, "claude", bus.PRESENCE_TTL_SECONDS + 10)
        unrelated = self.window("active", "claude", "claude-other", "routing")
        message_id = self.send()
        self.assertEqual(self.ids(unrelated, "claude"), [])
        self.assertEqual(self.ids(target, "claude"), [message_id])

    def test_reaped_window_is_not_an_automatic_target(self):
        """Verify reaped window is not an automatic target."""
        target = self.window("gone", "claude", "claude-routing")
        self.age(target, "claude", bus.PRESENCE_REAP_SECONDS + 10)
        self.assert_rejected_without_append()


class TestDirectRouting(RoutingFixture):
    """Verify explicit handles, broadcasts, and replies remain stable."""

    def test_explicit_handle_reaches_another_job(self):
        """Verify explicit handle reaches another job."""
        target = self.window("unrelated", "claude", "claude-other", "other")
        message_id = self.send(bus.current_handle(target, "claude"))
        self.assertEqual(self.ids(target, "claude"), [message_id])

    def test_absent_explicit_handle_waits_for_its_window(self):
        """Verify absent explicit handle waits for its window."""
        message_id = self.send("claude-later-001")
        target = self.window("later", "claude", "claude-later-001", "other")
        self.assertEqual(self.ids(target, "claude"), [message_id])

    def test_duplicate_explicit_handle_is_ambiguous_even_when_one_is_idle(
            self):
        """Verify idle duplicate handles still make delivery ambiguous."""
        older = self.window("older", "claude", "claude-shared-001")
        self.age(older, "claude", bus.PRESENCE_TTL_SECONDS + 10)
        self.window("newer", "claude", "claude-shared-001")
        self.assert_rejected_without_append("claude-shared-001")

    def test_reply_returns_to_original_sender_after_rename_and_job_change(
            self):
        """Verify replies survive sender name and job changes."""
        requester = self.window(
            "requester",
            "claude",
            "claude-request",
            "other")
        decoy = self.window("decoy", "claude", "claude-routing", "routing")
        original = messaging.send_direct(
            requester, "claude", bus.current_handle(
                self.sender, "codex"), "question")
        self.assertEqual(self.ids(self.sender, "codex"), [original])
        bus.set_name(requester, "claude-new-task")
        bus.set_job(requester, "new-job")
        bus.touch(requester, "claude")
        reply = self.send(reply_to=original)
        self.assertEqual(self.ids(decoy, "claude"), [])
        self.assertEqual(self.ids(requester, "claude"), [reply])

    def test_cli_broadcast_reaches_every_target_window(self):
        """Verify cli broadcast reaches every target window."""
        first = self.window("first", "claude", "claude-first", "first-job")
        second = self.window("second", "claude", "claude-second", "second-job")
        other_cli = self.window("other-cli", "gemini", "gemini-routing")
        message_id = self.send(broadcast=True)
        self.assertEqual(self.ids(first, "claude"), [message_id])
        self.assertEqual(self.ids(second, "claude"), [message_id])
        self.assertEqual(self.ids(other_cli, "gemini"), [])

    def test_machine_broadcast_reaches_each_agent_type(self):
        """Verify machine broadcast reaches each agent type."""
        windows = [
            (self.window(
                agent + "-other",
                agent,
                agent + "-other",
                "other"),
                agent) for agent in (
                "codex",
                "claude",
                "gemini")]
        message_id = self.send("*", broadcast=True)
        for client, agent in windows:
            with self.subTest(agent=agent):
                self.assertEqual(self.ids(client, agent), [message_id])

    def test_named_sender_without_presence_survives_roster_lookup(self):
        """Verify named sender without presence survives roster lookup."""
        target = self.window(
            "related",
            "claude",
            "claude-unregistered",
            "other")
        sender = bus.connect(self.directory, session="unregistered",
                             cwd=os.path.join(self.directory, "unregistered"))
        bus.set_job(sender, "unregistered-job")
        published = bus.set_name(sender, "codex-unregistered")
        # A new process has no cached Bus.handle; it relies on the file.
        sender = bus.connect(
            self.directory,
            session=sender.session,
            cwd=sender.cwd)
        record = messaging.send_direct(
            sender,
            "codex",
            "claude",
            "first send",
            return_record=True)
        reopened = bus.connect(
            self.directory,
            session=sender.session,
            cwd=sender.cwd)
        self.assertEqual(bus.current_handle(reopened, "codex"), published)
        self.assertEqual(record["from_handle"], published)
        self.assertEqual(self.ids(target, "claude"), [record["id"]])

    def test_matching_generated_handle_suffix_is_not_a_task_match(self):
        """Verify matching generated handle suffix is not a task match."""
        sender = self.window("sender-abcd", "codex", job="one-job")
        self.window("receiver-abcd", "claude", job="another-job")
        before = pathlib.Path(sender.path).read_bytes()
        with self.assertRaises(ValueError):
            messaging.send_direct(
                sender, "codex", "claude", "unrelated default names")
        self.assertEqual(pathlib.Path(sender.path).read_bytes(), before)

    def test_failed_resolution_then_explicit_retry_appends_one_message(self):
        """Verify failed resolution then explicit retry appends one message."""
        target = self.window("first", "claude", "claude-routing")
        other = self.window("second", "claude", "claude-routing")
        self.assert_rejected_without_append()
        message_id = self.send(bus.current_handle(target, "claude"))
        self.assertEqual(len(pathlib.Path(self.sender.path).read_text(
            encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.ids(other, "claude"), [])
        self.assertEqual(self.ids(target, "claude"), [message_id])

    def test_pinned_message_follows_session_when_handle_is_reused(self):
        """Verify pinned message follows session when handle is reused."""
        original = self.window("original", "claude", "claude-original-001")
        old_handle = bus.current_handle(original, "claude")
        message_id = self.send(old_handle)
        bus.set_name(original, "claude-renamed")
        replacement = self.window("replacement", "claude", old_handle)
        self.assertEqual(self.ids(replacement, "claude"), [])
        self.assertEqual(self.ids(original, "claude"), [message_id])

    def test_pinned_message_survives_claude_process_to_hook_binding(self):
        """Verify pinned messages follow the hook conversation."""
        legacy = self.window("claude200", "claude", "claude-routing")
        with test_utils.process_generation('1000'):
            record = self.send(return_record=True)
        self.assertEqual(record["to_session"], legacy.session)
        canonical = bus.connect(self.directory, session="claude-conversation",
                                cwd=legacy.cwd)
        with (
            test_utils.process_identity(
                pid=200, agent='claude', generation='1000'),
        ):
            bus.bind_session(canonical, "claude")
            bus.register(canonical, "claude")
            self.assertEqual(self.ids(canonical, "claude"), [record["id"]])
            self.assertEqual(self.ids(canonical, "claude"), [])

    def test_bound_pin_survives_process_exit_and_pid_reuse(self):
        """Verify bound pins survive process exit and pid reuse."""
        legacy = self.window("claude200", "claude", "claude-routing")
        canonical = bus.connect(
            self.directory,
            session="original-conversation",
            cwd=legacy.cwd)
        with (
            test_utils.process_identity(
                pid=200, agent='claude', generation='1000'),
        ):
            record = self.send(return_record=True)
            bus.bind_session(canonical, "claude")
            bus.register(canonical, "claude")
        with test_utils.process_generation(None):
            self.assertTrue(routing.addressed_to(canonical, "claude", record))
        replacement = bus.connect(
            self.directory,
            session="replacement-conversation",
            cwd=legacy.cwd)
        with (
            test_utils.process_identity(
                pid=200, agent='claude', generation='2000'),
        ):
            bus.bind_session(replacement, "claude")
            bus.register(replacement, "claude")
            self.assertEqual(self.ids(replacement, "claude"), [])
            self.assertEqual(self.ids(canonical, "claude"), [record["id"]])

    def test_unbound_reused_pid_cannot_read_old_pin_or_reply(self):
        """Verify reused pids cannot read earlier pins or replies."""
        original = self.window("claude200", "claude", "claude-routing")
        with test_utils.process_generation('1000'):
            incoming = messaging.send_direct(
                original,
                "claude",
                bus.current_handle(
                    self.sender,
                    "codex"),
                "original question",
                return_record=True)
            waiting = self.send(return_record=True)
        with test_utils.process_generation('2000'):
            reply = self.send(reply_to=incoming["id"], return_record=True)
            self.assertFalse(routing.addressed_to(original, "claude", waiting))
            self.assertFalse(routing.addressed_to(original, "claude", reply))
            self.assertEqual(self.ids(original, "claude"), [])


if __name__ == "__main__":
    unittest.main()
