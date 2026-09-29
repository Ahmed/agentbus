"""Regressions for the periodic name check the session hook injects."""

import json
import os
import subprocess
import sys
import unittest
import unittest.mock as mock

import agentbus_messages as messaging
import bus
import session_hook
import tests.agentbus_test_utils as test_utils

# Invoked by path rather than -m, because that is how the CLI invokes it.
HOOK = session_hook.__file__


class TestTurnCounter(test_utils.BusFixture):
    """Verify the counter fires on the interval and survives damage."""

    def setUp(self):
        """Open one window whose turn counter the tests drive directly."""
        super().setUp()
        self.client = self.window("window", "claude", "claude-login")

    def test_fires_on_the_tenth_turn_and_not_before(self):
        """Verify nine quiet turns precede the check on the tenth."""
        fired = [session_hook._count_turn(self.client) for _ in range(10)]
        self.assertEqual(fired, [False] * 9 + [True])

    def test_interval_restarts_after_firing(self):
        """Verify the next check is a full interval after the last."""
        for _ in range(10):
            session_hook._count_turn(self.client)
        fired = [session_hook._count_turn(self.client) for _ in range(10)]
        self.assertEqual(fired, [False] * 9 + [True])

    @mock.patch.object(session_hook, "RENAME_EVERY_TURNS", 0)
    def test_zero_interval_never_fires(self):
        """Verify the check can be switched off entirely."""
        self.assertEqual(
            [session_hook._count_turn(self.client) for _ in range(30)],
            [False] * 30)
        self.assertFalse(os.path.exists(session_hook._turns_path(self.client)))

    def test_corrupt_counter_starts_over_rather_than_raising(self):
        """Verify a damaged counter costs a late check, not the hook."""
        with open(session_hook._turns_path(self.client), "w",
                  encoding="utf-8") as handle:
            handle.write("not a number")
        self.assertFalse(session_hook._count_turn(self.client))

    def test_counter_is_per_session(self):
        """Verify two windows on one bus do not share an interval."""
        other = self.window("other", "codex", "codex-export")
        for _ in range(9):
            session_hook._count_turn(self.client)
        self.assertFalse(session_hook._count_turn(other))
        self.assertTrue(session_hook._count_turn(self.client))


class TestNameCheckText(test_utils.BusFixture):
    """Verify the injected check says what this window is published as."""

    def test_names_the_current_handle_job_and_commands(self):
        """Verify the check carries the facts a rename decision needs."""
        client = self.window("window", "claude", "claude-login", "auth@main")
        text = session_hook._name_check(client, "claude")
        self.assertIn(bus.current_handle(client, "claude"), text)
        self.assertIn("auth@main", text)
        self.assertIn("task 'login'", text)
        self.assertIn("name <task>", text)
        self.assertIn("job <project>", text)


# Openings of turns the operator did not type, one per kind.
BUS_PROMPTS = (
    "You are on the agent bus as **codex**. The other coding agents",
    "You are **codex** on the agent bus. The other coding agents",
    "Agent bus: an automatic inbox check was requested for this window.",
    "<task-notification>\n<task-id>b1</task-id>",
)


class TestOperatorPrompt(unittest.TestCase):
    """Verify turns the bus started are told apart from typed ones."""

    def test_turns_the_bus_started_are_not_the_operator(self):
        """Verify the brief, a wake and a listener return are skipped."""
        for prompt in BUS_PROMPTS:
            with self.subTest(prompt=prompt):
                self.assertFalse(
                    session_hook._operator_prompt({"prompt": prompt}))

    def test_typed_and_unreported_prompts_are_the_operator(self):
        """Verify real work, and a CLI that sends no prompt, still count."""
        for payload in ({"prompt": "Fix the login redirect loop"},
                        {"prompt": "You are right, try the other branch"},
                        {}):
            with self.subTest(payload=payload):
                self.assertTrue(session_hook._operator_prompt(payload))


class HookFixture(test_utils.BusFixture):
    """Drive the real hook script for one window.

    Driven through the real script rather than by calling main() in
    process, because what is under test is the wiring: which events
    count, and that one hook run emits one block carrying everything it
    had to say.

    Attributes:
        TASK (str or None): Task the driven window declares, if any.
    """

    TASK = "login"

    def setUp(self):
        """Open the window whose turns the hook invocations simulate."""
        super().setUp()
        if self.TASK:
            self.client = self.window("window", "claude", self.TASK)
            return
        self.client = bus.connect(
            self.directory, session="window",
            cwd=os.path.join(self.directory, "window"))
        bus.set_job(self.client, self.DEFAULT_JOB)
        bus.register(self.client, "claude")

    def hook(self, event="UserPromptSubmit", prompt=None):
        """Run one hook invocation and return what it injected.

        Args:
            event (str): Hook event name the CLI would have sent.
            prompt (str or None): Prompt text, or None to send none.

        Returns:
            str: Injected context, or "" when the hook stayed silent.
        """
        payload = {"hook_event_name": event,
                   "session_id": self.client.session,
                   "cwd": self.client.cwd,
                   "prompt_id": "chain"}
        if prompt is not None:
            payload["prompt"] = prompt
        payload = json.dumps(payload)
        environment = dict(os.environ, AGENTBUS_DIR=self.directory,
                           AGENTBUS_WATCHER="0")
        result = subprocess.run(
            [sys.executable, HOOK, "--agent", "claude"],
            input=payload, capture_output=True, text=True,
            env=environment, check=True)
        if not result.stdout.strip():
            return ""
        emitted = json.loads(result.stdout)
        return (emitted.get("hookSpecificOutput", {}).get("additionalContext")
                or emitted.get("reason") or "")

    def turns(self, count, event="UserPromptSubmit"):
        """Run several hook invocations and collect their output.

        Args:
            count (int): How many turns to simulate.
            event (str): Hook event name for each of them.

        Returns:
            list[str]: Injected context per turn, "" where silent.
        """
        return [self.hook(event) for _ in range(count)]


class TestNameCheckDelivery(HookFixture):
    """Verify the check reaches the model without displacing mail."""

    def test_silent_until_the_interval_then_injected(self):
        """Verify ordinary turns inject nothing at all."""
        injected = self.turns(10)
        self.assertEqual(injected[:9], [""] * 9)
        self.assertIn("on the roster as", injected[9])

    def test_only_turn_starts_count(self):
        """Verify per-tool hooks do not spend the interval."""
        self.assertEqual(self.turns(12, event="PostToolUse"), [""] * 12)
        self.assertEqual(self.turns(9), [""] * 9)
        self.assertIn("on the roster as", self.hook())

    def test_mail_and_check_arrive_together(self):
        """Verify a check due on a turn with mail loses neither.

        The mail is posted after the quiet turns, because an earlier
        hook would simply have delivered it and left this one nothing
        to collide with.
        """
        sender = self.window("sender", "codex", "codex-login")
        self.turns(9)
        messaging.send_direct(sender, "codex",
                              bus.current_handle(self.client, "claude"),
                              "the export is ready")
        injected = self.hook()
        self.assertIn(bus.current_handle(sender, "codex"), injected)
        self.assertIn("on the roster as", injected)

    def test_turns_the_bus_started_do_not_count(self):
        """Verify only typed prompts spend the interval."""
        self.assertEqual(self.turns(9), [""] * 9)
        for prompt in BUS_PROMPTS:
            self.assertEqual(self.hook(prompt=prompt), "")
        self.assertIn("on the roster as", self.hook(prompt="next step"))


class TestFirstTaskQuestion(HookFixture):
    """Verify a window with no task yet is asked once, on its first task."""

    TASK = None

    def test_asked_on_the_first_typed_prompt_only(self):
        """Verify the brief does not use up the question; the task does."""
        self.assertEqual(self.hook(prompt=BUS_PROMPTS[0]), "")

        first = self.hook(prompt="Add retries to the export job")

        handle = bus.current_handle(self.client, "claude")
        self.assertIn(f"{handle!r} with no task", first)
        self.assertIn("name <task>", first)
        self.assertEqual(self.hook(prompt="and log each attempt"), "")

    def test_a_window_that_declared_its_task_is_not_asked(self):
        """Verify a window that already said what it is on is left alone."""
        bus.set_task(self.client, "export-retries")

        self.assertEqual(self.hook(prompt="Add retries to the export job"),
                         "")

    @mock.patch.object(bus, "current_task", return_value="inbox")
    def test_a_task_about_reading_mail_counts_as_none(self, _task):
        """Verify windows that declared one before the refusal are asked."""
        self.assertTrue(session_hook._no_task(self.client, "codex"))


class TestWakeupMessage(unittest.TestCase):
    """Verify the wake-up message gives the right instructions."""

    def test_woken_preamble_has_reply_instructions(self):
        """Verify a woken agent is told how to reply on the bus."""
        agent_name = "claude"
        messages = [
            {"kind": "message", "from_handle": "codex-802", "text": "hello"}
        ]

        preamble = session_hook._preamble(agent_name, messages, woken=True)

        self.assertIn("python3", preamble)
        self.assertIn(f"bus.py send {agent_name} codex-802", preamble)
        self.assertIn("report_result tool with the task's id", preamble)
        self.assertIn("Before you stop, tell the operator", preamble)


if __name__ == "__main__":
    unittest.main()
