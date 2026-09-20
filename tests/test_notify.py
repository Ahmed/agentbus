"""Doorbell regressions: rings reach a waiter, and absence costs nothing.

Split in two on purpose. The first class needs no Redis at all and must
pass anywhere, because the whole promise of this module is that a bus
with no doorbell still works. The second only runs where a Redis is
actually reachable, since a ring that nobody can publish proves nothing.
"""

import threading
import time
import unittest
import unittest.mock as mock

import agentbus_notify as notify
import bus
import session_hook
import tests.agentbus_test_utils as fixtures


def _redis_available():
    """Is there a Redis this machine can actually reach?

    Returns:
        bool: True when a connection was opened and answered.
    """
    notify.reset()
    return notify.available()


class NotifyWithoutRedisTests(unittest.TestCase):
    """The doorbell must be optional, and silently so."""

    def setUp(self):
        """Forget any connection a previous test opened."""
        super().setUp()
        notify.reset()
        self.addCleanup(notify.reset)

    def test_channels_name_the_session_before_the_cli(self):
        """A known session is addressed ahead of the whole CLI."""
        self.assertEqual(
            notify.channels_for("codex", "abc"),
            ["agentbus:session:abc", "agentbus:agent:codex"])

    def test_channels_fall_back_to_the_cli_when_session_is_unknown(self):
        """Mail for an unregistered handle can only reach every window."""
        self.assertEqual(notify.channels_for("codex", None),
                         ["agentbus:agent:codex"])

    def test_nothing_to_address_produces_no_channels(self):
        """A record naming nobody rings nobody."""
        self.assertEqual(notify.channels_for("", None), [])

    @mock.patch.object(notify, "redis_client", None)
    def test_missing_library_disables_the_doorbell(self):
        """With no redis module installed the doorbell is simply off."""
        self.assertFalse(notify.enabled())

    @mock.patch.dict("os.environ", {"AGENTBUS_REDIS": "0"})
    def test_switching_it_off_is_honoured(self):
        """AGENTBUS_REDIS=0 turns it off even where Redis is running."""
        self.assertFalse(notify.enabled())

    @mock.patch.dict("os.environ", {"AGENTBUS_REDIS": "0"})
    def test_waiting_without_a_doorbell_returns_at_once(self):
        """A disabled doorbell reports no ring rather than blocking."""
        started = time.time()

        answered = notify.wait("codex", "abc", 30)

        self.assertFalse(answered)
        self.assertLess(time.time() - started, 5)

    @mock.patch.dict("os.environ", {"AGENTBUS_REDIS": "0"})
    def test_ringing_without_a_doorbell_is_a_no_op(self):
        """Sending must not fail because there is nowhere to ring."""
        notify.ring({"to_agent": "codex", "to_session": "abc"})

    @mock.patch.object(notify, "_client")
    def test_a_refusing_redis_does_not_reach_the_sender(self, client):
        """A publish that raises is swallowed, not passed to the send."""
        client.return_value.publish.side_effect = OSError("gone")

        notify.ring({"to_agent": "codex", "to_session": "abc"})

        client.return_value.publish.assert_called()


@unittest.skipUnless(_redis_available(), "no reachable Redis")
class NotifyWithRedisTests(fixtures.BusFixture):
    """With a real Redis, a send must wake a waiter immediately."""

    TEMP_PREFIX = "agentbus-notify-test-"
    DEFAULT_JOB = "notify"

    def setUp(self):
        """Open the two windows the ring travels between."""
        super().setUp()
        self.sender = self.window("sender", "codex", "codex-notify")
        self.target = self.window("target", "claude", "claude-notify")
        self.answered = None

    def listen(self, seconds):
        """Wait for a ring aimed at the target window.

        Args:
            seconds (float): Longest time to listen.
        """
        self.answered = notify.wait("claude", self.target.session, seconds)

    def test_a_send_wakes_a_waiting_window(self):
        """The waiter returns because the send rang, not because it polled."""
        listener = threading.Thread(target=self.listen, args=(10,))
        listener.start()
        time.sleep(0.5)

        bus.send(self.sender, "codex", "claude-notify-001", "wake up")
        listener.join(timeout=10)

        self.assertFalse(listener.is_alive())
        self.assertTrue(self.answered)

    def test_a_quiet_bus_times_out_rather_than_waking(self):
        """No send means no ring, and the waiter gives up on its own."""
        started = time.time()

        answered = notify.wait("claude", self.target.session, 1)

        self.assertFalse(answered)
        self.assertGreaterEqual(time.time() - started, 0.9)

    def send_late(self, delay):
        """Send to the target after a pause, from another thread.

        Args:
            delay (float): Seconds to wait before sending.
        """
        time.sleep(delay)
        bus.send(self.sender, "codex", "claude-notify-001", "late mail")

    def test_stop_hook_holds_the_turn_open_for_late_mail(self):
        """Mail arriving after a turn ends still reaches that window.

        This is the whole point of the listen. Without it the window
        goes idle the instant the turn stops and this message waits for
        somebody to type; with it the turn is still in its hook, so the
        mail is found and the turn restarts.
        """
        sender = threading.Thread(target=self.send_late, args=(0.5,))
        sender.start()
        self.addCleanup(sender.join, 10)

        waiting = session_hook.waiting_for_wake(self.target, "claude", 10)

        self.assertTrue(waiting)

    def test_stop_hook_does_not_hold_when_the_listen_is_off(self):
        """With the listen disabled the hook returns to its old behaviour."""
        started = time.time()

        waiting = session_hook.waiting_for_wake(self.target, "claude", 0)

        self.assertEqual(waiting, [])
        self.assertLess(time.time() - started, 1)

    @mock.patch.dict("os.environ", {"AGENTBUS_STOP_WAIT": ""})
    def test_codex_listens_by_default_and_others_do_not(self):
        """Only the CLI whose hook timeout allows it holds a turn open.

        The environment override is cleared here because the suite sets
        it to zero everywhere else, and what is under test is the
        built-in default rather than the override.
        """
        self.assertGreater(session_hook.stop_wait("codex"), 0)
        self.assertEqual(session_hook.stop_wait("gemini"), 0)
        self.assertEqual(session_hook.stop_wait("claude"), 0)

    def test_a_ring_for_another_window_is_not_ours(self):
        """A message addressed elsewhere must not wake this window."""
        listener = threading.Thread(target=self.listen, args=(2,))
        listener.start()
        time.sleep(0.5)

        notify.ring({"to_agent": "gemini", "to_session": "somebody-else"})
        listener.join(timeout=10)

        self.assertFalse(self.answered)


if __name__ == "__main__":
    unittest.main()
