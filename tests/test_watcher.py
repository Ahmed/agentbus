"""Keep queued wakes bounded when hooks consume mail during an active turn."""

import json
import pathlib
import subprocess
import time
import unittest
import unittest.mock as mock

import agentbus_messages as messaging
import bus
import tests.agentbus_test_utils as fixtures
import watcher


class TestWakeScheduling(fixtures.BusFixture):
    """Exercise real temporary inboxes without queuing any live agent turns."""

    TEMP_PREFIX = "agentbus-wake-test-"
    DEFAULT_JOB = "watcher-regression"

    def setUp(self):
        """Keep scheduling cases separate from live mail and other windows."""
        super().setUp()
        self.sender = self.window("sender", "claude", "claude-wake-sender")
        self.target = self.window("target", "codex", "codex-wake-target")
        self.presence_path = pathlib.Path(
            self.target.state) / "presence.codex.target"
        self.notice_path = pathlib.Path(
            self.target.state) / "wake_notice.codex.target"

    def presence(self, status="idle", age=1):
        """Model a settled heartbeat without waiting for a real idle window.

        Args:
            status (str): Hook-reported activity state.
            age (float): Seconds since that heartbeat.

        Returns:
            dict: Recorded presence, retained for non-mutation assertions.
        """
        record = json.loads(self.presence_path.read_text())
        record.update(status=status, last_seen=time.time() - age)
        self.presence_path.write_text(json.dumps(record))
        return record

    def mail(self, kind="message"):
        """Route through a temporary bus to keep unread checks realistic.

        Args:
            kind (str): Envelope kind whose wake eligibility is being tested.

        Returns:
            list[dict]: Unread messages from the isolated recipient inbox.
        """
        messaging.send_direct(self.sender, "claude",
                              bus.current_handle(self.target, "codex"),
                              "isolated scheduling probe", kind=kind)
        return bus.peek(self.target, "codex")

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_busy_mail_is_left_to_hooks(self, wake):
        """Busy turns must not queue prompts for mail their hooks can read."""
        waiting = self.mail()
        self.presence("busy")

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(accepted)
        wake.assert_not_called()
        self.assertEqual(bus.peek(self.target, "codex"), waiting)
        self.assertFalse(self.notice_path.exists())

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_idle_heartbeat_must_settle(self, wake):
        """Let a Stop hook finish delivery before queuing an idle wake."""
        waiting = self.mail()
        self.presence(age=0)

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(accepted)
        wake.assert_not_called()
        self.assertEqual(bus.peek(self.target, "codex"), waiting)

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_unknown_presence_does_not_queue(self, wake):
        """A missing heartbeat is not proof that a session is safe to wake."""
        waiting = self.mail()
        self.presence_path.unlink()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(accepted)
        wake.assert_not_called()
        self.assertFalse(self.presence_path.exists())

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_invalid_presence_does_not_queue(self, wake):
        """A corrupt heartbeat must not imply that a window is idle."""
        waiting = self.mail()
        self.presence_path.write_text('{"status":')

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(accepted)
        wake.assert_not_called()

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_idle_wake_preserves_mail_and_presence(self, wake):
        """Request delivery without consuming mail or updating presence."""
        waiting = self.mail()
        before = self.presence()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertTrue(accepted)
        wake.assert_called_once_with("codex", "target", waiting)
        self.assertEqual(bus.peek(self.target, "codex"), waiting)
        self.assertEqual(json.loads(self.presence_path.read_text()), before)
        self.assertEqual(json.loads(self.notice_path.read_text())[
                         "idle_epoch"], before["last_seen"])

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_busy_mail_can_wake_after_window_becomes_idle(self, wake):
        """Deferral must preserve the chance to wake for unread work."""
        waiting = self.mail()
        self.presence("busy")
        deferred = watcher.wake_if_idle(self.target, "codex", waiting)
        self.presence()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(deferred)
        self.assertTrue(accepted)
        wake.assert_called_once_with("codex", "target", waiting)

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_burst_uses_one_wake_per_idle_period(self, wake):
        """A burst shares one wake instead of adding queued prompts."""
        first = self.mail()
        self.presence()
        first_accepted = watcher.wake_if_idle(self.target, "codex", first)
        burst = self.mail()

        second_accepted = watcher.wake_if_idle(self.target, "codex", burst)

        self.assertTrue(first_accepted)
        self.assertFalse(second_accepted)
        wake.assert_called_once_with("codex", "target", first)
        self.assertEqual(len(bus.peek(self.target, "codex")), 2)

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_restart_keeps_the_outstanding_wake_claim(self, wake):
        """A restarted observer must retain the pending wake claim."""
        waiting = self.mail()
        self.presence()
        first_accepted = watcher.wake_if_idle(self.target, "codex", waiting)
        restarted = bus.connect(
            self.directory,
            session="target",
            cwd=self.target.cwd)

        second_accepted = watcher.wake_if_idle(restarted, "codex", waiting)

        self.assertTrue(first_accepted)
        self.assertFalse(second_accepted)
        wake.assert_called_once_with("codex", "target", waiting)

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_a_later_idle_period_can_receive_new_mail(self, wake):
        """Coalescing one burst must not disable future idle delivery."""
        first = self.mail()
        self.presence()
        first_accepted = watcher.wake_if_idle(self.target, "codex", first)
        received = bus.receive(self.target, "codex")
        self.presence("busy")
        second = self.mail()
        self.presence(age=0.75)

        second_accepted = watcher.wake_if_idle(self.target, "codex", second)

        self.assertTrue(first_accepted)
        self.assertTrue(second_accepted)
        self.assertEqual(received, first)
        self.assertEqual(wake.call_args_list,
                         [mock.call("codex", "target", first),
                          mock.call("codex", "target", second)])

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_consumed_mail_is_rechecked_before_queuing(self, wake):
        """Recheck mail that a hook consumed after the first peek."""
        waiting = self.mail()
        received = bus.receive(self.target, "codex")
        self.presence()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertEqual(received, waiting)
        self.assertFalse(accepted)
        wake.assert_not_called()
        self.assertEqual(bus.peek(self.target, "codex"), [])

    @mock.patch.object(watcher, "wake", return_value=True)
    @mock.patch.object(watcher, "_idle_epoch", side_effect=[1.0, None])
    def test_window_becoming_busy_cancels_the_attempt(self, idle_epoch, wake):
        """Rechecking activity closes the ordinary peek-to-queue race."""
        waiting = self.mail()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(accepted)
        self.assertEqual(idle_epoch.call_args_list,
                         [mock.call(self.target, "codex"),
                          mock.call(self.target, "codex")])
        wake.assert_not_called()
        self.assertFalse(self.notice_path.exists())

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_receipts_do_not_wake_or_ring(self, wake):
        """A delivery acknowledgement is not new work for a sleeping agent."""
        waiting = self.mail("ack")
        self.presence()
        first_seen = {waiting[0]["id"]: time.time() - 10}
        announced = set()

        due = watcher.due_messages(waiting, first_seen, announced)
        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertEqual(due, ([], []))
        self.assertEqual(first_seen, {})
        self.assertFalse(accepted)
        wake.assert_not_called()
        self.assertEqual(bus.peek(self.target, "codex"), waiting)

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_project_checks_start_idle_turns(self, wake):
        """The first project question must reach an idle recipient."""
        waiting = self.mail("project_check")
        self.presence()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertTrue(accepted)
        wake.assert_called_once_with("codex", "target", waiting)
        self.assertEqual(bus.peek(self.target, "codex"), waiting)

    @mock.patch.object(watcher, "wake", return_value=True)
    def test_project_status_does_not_wake_or_ring(self, wake):
        """Confirmation bookkeeping must not start a conversation."""
        waiting = self.mail("project_status")
        self.presence()

        due = watcher.due_messages(waiting, {}, set())
        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertEqual(due, ([], []))
        self.assertFalse(accepted)
        wake.assert_not_called()

    @mock.patch.object(watcher, "wake", side_effect=[False, True])
    def test_explicit_queue_rejection_can_retry(self, wake):
        """A definite queue refusal must leave a retry possible."""
        waiting = self.mail()
        self.presence()
        refused = watcher.wake_if_idle(self.target, "codex", waiting)
        claim_after_refusal = self.notice_path.exists()

        accepted = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(refused)
        self.assertFalse(claim_after_refusal)
        self.assertTrue(accepted)
        self.assertEqual(wake.call_count, 2)
        wake.assert_called_with("codex", "target", waiting)

    @mock.patch.object(watcher, "wake", return_value=None)
    def test_unknown_queue_outcome_keeps_its_claim(self, wake):
        """An uncertain queue outcome must not create a duplicate prompt."""
        waiting = self.mail()
        self.presence()
        uncertain = watcher.wake_if_idle(self.target, "codex", waiting)

        retried = watcher.wake_if_idle(self.target, "codex", waiting)

        self.assertFalse(uncertain)
        self.assertFalse(retried)
        self.assertTrue(self.notice_path.exists())
        wake.assert_called_once_with("codex", "target", waiting)
        self.assertEqual(bus.peek(self.target, "codex"), waiting)


class TestWakeTiming(unittest.TestCase):
    """Keep deferred mail from causing a busy polling loop."""

    def test_overdue_deferred_mail_does_not_spin(self):
        """Overdue mail waits for a state change instead of polling."""
        first_seen = {"mail": time.time() - 10}

        delay = watcher.next_look(first_seen, set())

        self.assertIsNone(delay)

    def test_no_due_mail_blocks_indefinitely(self):
        """An empty inbox must park on the doorbell instead of polling."""
        delay = watcher.next_look({}, set())

        self.assertIsNone(delay)

    @mock.patch.object(watcher.shutil, "which", return_value="/mock/codex")
    @mock.patch.object(watcher.subprocess, "run",
                       side_effect=subprocess.TimeoutExpired("codex", 15))
    def test_queue_timeout_has_an_unknown_outcome(self, run, which):
        """A missing reply does not establish that no prompt was queued."""
        result = watcher.wake("codex", "thread", [{"id": "mail"}])

        self.assertIsNone(result)
        which.assert_called_once_with("codex")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:4],
                         ["codex", "queue", "--thread", "thread"])


if __name__ == "__main__":
    unittest.main()
