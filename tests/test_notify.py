"""Doorbell regressions: rings reach a waiter, and absence costs nothing.

Split in two on purpose. The first class needs no Redis at all and must
pass anywhere, because the whole promise of this module is that a bus
with no doorbell still works. The second only runs where a Redis is
actually reachable, since a ring that nobody can publish proves nothing.
"""

import os
import threading
import time
import unittest
import unittest.mock as mock

import agentbus_notify as notify
import bus
import session_hook
import tests.agentbus_test_utils as fixtures
import watcher


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
    def test_no_cli_holds_a_turn_open_by_default(self):
        """Ending a turn must not cost a window time it will not use.

        This held codex turns open for eight seconds each, paid on every
        turn that ended with an empty inbox whether or not any mail was
        coming. The environment override is cleared here because the
        suite sets it to zero everywhere else, and what is under test is
        the built-in default.
        """
        self.assertEqual(session_hook.stop_wait("codex"), 0)
        self.assertEqual(session_hook.stop_wait("gemini"), 0)
        self.assertEqual(session_hook.stop_wait("claude"), 0)

    @mock.patch.dict("os.environ", {"AGENTBUS_STOP_WAIT": "3"})
    def test_the_hold_can_still_be_asked_for(self):
        """Anyone who wants the belt as well as the braces may have it."""
        self.assertEqual(session_hook.stop_wait("codex"), 3)

    def test_a_ring_for_another_window_is_not_ours(self):
        """A message addressed elsewhere must not wake this window."""
        listener = threading.Thread(target=self.listen, args=(2,))
        listener.start()
        time.sleep(0.5)

        notify.ring({"to_agent": "gemini", "to_session": "somebody-else"})
        listener.join(timeout=10)

        self.assertFalse(self.answered)


class FallbackSleepTests(unittest.TestCase):
    """A doorbell that cannot be reached must not become a spin."""

    def setUp(self):
        """Forget any connection a previous test opened."""
        super().setUp()
        notify.reset()
        self.addCleanup(notify.reset)

    @mock.patch.object(notify, "REDIS_URL", "redis://127.0.0.1:6399/5")
    def test_an_unreachable_server_still_costs_the_floor(self):
        """Installed library, dead server: the ordinary way Redis fails.

        wait() answers "no ring" instantly here, which is correct and
        ruinous in a loop -- the caller would spin at full tilt for as
        long as the server stayed down. The floor is what stops that.
        """
        started = time.time()

        rang = notify.wait_or_sleep("claude", "session", 5.0, 0.3)

        self.assertFalse(rang)
        self.assertGreaterEqual(time.time() - started, 0.25)
        self.assertLess(time.time() - started, 3)

    @mock.patch.dict("os.environ", {"AGENTBUS_REDIS": "0"})
    def test_a_disabled_doorbell_also_costs_the_floor(self):
        """Switched off is the same shape of problem as unreachable."""
        started = time.time()

        rang = notify.wait_or_sleep("claude", "session", 20.0, 0.3)

        self.assertFalse(rang)
        self.assertGreaterEqual(time.time() - started, 0.25)

    @mock.patch.dict("os.environ", {"AGENTBUS_REDIS": "0"})
    def test_the_floor_never_outlasts_the_timeout(self):
        """A caller asking for a short wait must not be held longer."""
        started = time.time()

        notify.wait_or_sleep("claude", "session", 0.2, 30.0)

        self.assertLess(time.time() - started, 5)


class WindowNameTests(fixtures.BusFixture):
    """Every window gets a name that says something about its work."""

    TEMP_PREFIX = "agentbus-name-test-"
    DEFAULT_JOB = "agentbus@numbered-window-names"

    def test_the_branch_is_preferred_over_the_repository(self):
        """Two windows on one repo are common; one branch is the split."""
        self.assertEqual(
            session_hook.name_from_job("claude", "agentbus@my-branch"),
            "claude-my-branch")

    def test_a_job_without_a_branch_uses_the_repository(self):
        """A bare job name is still better than four hex characters."""
        self.assertEqual(
            session_hook.name_from_job("codex", "scratchpad"),
            "codex-scratchpad")

    def test_awkward_characters_become_a_usable_name(self):
        """A branch name is not required to be a legal handle."""
        self.assertEqual(
            session_hook.name_from_job("claude", "repo@feature/JIRA-9_x"),
            "claude-feature-jira-9-x")

    def test_a_long_branch_is_trimmed_to_leave_room_for_a_number(self):
        """The name must still fit once a three-digit suffix is added."""
        name = session_hook.name_from_job(
            "claude", "repo@" + "a-very-long-branch-name" * 3)

        self.assertLessEqual(len(name), bus.NAME_STEM_MAX)
        self.assertFalse(name.endswith("-"))

    def test_a_job_that_says_nothing_leaves_the_fallback(self):
        """With no job there is nothing better than the generated handle."""
        self.assertIsNone(session_hook.name_from_job("claude", ""))
        self.assertIsNone(session_hook.name_from_job("claude", "unknown"))

    def test_an_unnamed_window_is_named_after_its_job(self):
        """A window that never named itself still gets a real name."""
        window = bus.connect(self.directory, session="fresh-session",
                             cwd=os.path.join(self.directory, "fresh"))
        bus.set_job(window, self.DEFAULT_JOB)
        bus.register(window, "claude")

        named = session_hook.autoname(window, "claude")

        self.assertTrue(named.startswith("claude-numbered-window-names"))

    def test_a_window_that_chose_a_name_keeps_it(self):
        """Naming is a decision, and SessionStart must not overrule it."""
        window = self.window("chosen-session", "claude", "claude-sso-login")

        named = session_hook.autoname(window, "claude")

        self.assertEqual(named, "claude-sso-login-001")


class WakeTests(unittest.TestCase):
    """Starting a turn from outside, where a CLI allows it."""

    def test_only_codex_has_a_wake_command(self):
        """Claude and gemini have no way in, so they must not claim one."""
        self.assertIn("codex", watcher.WAKE_COMMANDS)
        self.assertNotIn("claude", watcher.WAKE_COMMANDS)
        self.assertNotIn("gemini", watcher.WAKE_COMMANDS)

    def test_a_cli_without_a_wake_is_not_woken(self):
        """An unknown CLI falls back to the bell rather than guessing."""
        self.assertFalse(watcher.wake("gemini", "session", [{"id": "a"}]))

    def test_a_window_with_no_session_is_not_woken(self):
        """Without a thread id there is nothing to address."""
        self.assertFalse(watcher.wake("codex", "", [{"id": "a"}]))

    @mock.patch.dict("os.environ", {"AGENTBUS_WAKE": "0"})
    def test_the_wake_can_be_switched_off(self):
        """AGENTBUS_WAKE=0 leaves the bell as the only signal."""
        self.assertFalse(watcher.wake("codex", "thread", [{"id": "a"}]))

    @mock.patch.object(watcher.subprocess, "run")
    @mock.patch.object(watcher.shutil, "which", return_value="/usr/bin/codex")
    def test_the_wake_names_the_thread_and_withholds_the_mail(
            self, _which, run):
        """It says mail is waiting; it never carries the mail itself."""
        run.return_value.returncode = 0

        woken = watcher.wake("codex", "thread-1",
                             [{"id": "a", "text": "SECRET PAYLOAD"}])

        self.assertTrue(woken)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["codex", "queue", "--thread", "thread-1"])
        self.assertNotIn("SECRET PAYLOAD", " ".join(argv))

    @mock.patch.object(watcher.subprocess, "run")
    @mock.patch.object(watcher.shutil, "which", return_value="/usr/bin/codex")
    def test_a_refusing_daemon_is_not_a_wake(self, _which, run):
        """A non-zero exit means the window was left for the bell."""
        run.return_value.returncode = 1

        self.assertFalse(watcher.wake("codex", "thread-1", [{"id": "a"}]))

    @mock.patch.object(watcher.shutil, "which", return_value=None)
    def test_a_missing_codex_binary_is_not_a_wake(self, _which):
        """Nothing to run means nothing claimed."""
        self.assertFalse(watcher.wake("codex", "thread-1", [{"id": "a"}]))


if __name__ == "__main__":
    unittest.main()
