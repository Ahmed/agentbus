"""Project-confirmation regressions using real sends on temporary buses."""

import concurrent.futures as futures
import json
import os
import pathlib
import stat
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock as mock

import agentbus_storage as storage
import bus
import project_confirmation
import tests.agentbus_test_utils as fixtures

# Captured before any test patches time.time, so a test that freezes the
# clock can still let it run normally for the part of itself that needs a
# real one.
REAL_TIME = time.time


class ConfirmationFixture(fixtures.BusFixture):
    """A sender and a target on one bus, and the words to make them talk.

    The marker is the thing every test in both suites is really about:
    it is the only text that must never reach the log, a peek or a
    metadata field before somebody has said yes.
    """

    TEMP_PREFIX = "agentbus-confirmation-test-"

    def setUp(self):
        """Open the two windows every confirmation test sends between."""
        super().setUp()
        self.sender = self.window("sender", "codex", "codex-project")
        self.target = self.window("target", "claude", "claude-project")
        self.marker = "HELD_PAYLOAD_MUST_NOT_BE_DISCLOSED"

    def send(self, to="claude", text=None, **kwargs):
        """Send from the sender, defaulting to the withheld marker text."""
        return bus.send(self.sender, "codex", to, text or self.marker,
                        return_record=True, **kwargs)

    def confirm(self, record, accept=True, client=None, agent="claude"):
        """Answer the check a send produced, as the target by default."""
        return project_confirmation.confirm_project(
            client or self.target, agent, record["confirmation_id"], accept)

    def messages(self, client, agent):
        """Everything waiting for one window, consumed as a read would."""
        return bus.receive(client, agent, limit=100)

    def log(self):
        """The shared log exactly as it sits on disk."""
        return pathlib.Path(self.sender.path).read_text(encoding="utf-8")

    def records(self):
        """The shared log parsed back into records."""
        return [json.loads(line) for line in self.log().splitlines()]

    def pending(self, record):
        """Where a held request's private file lives."""
        return (pathlib.Path(self.sender.state)
                / f"pending.{record['confirmation_id']}.json")

    def establish(self):
        """A confirmed pair, with both sides' mail already drained."""
        request = self.send()
        self.assertEqual(self.confirm(request)["status"], "confirmed")
        self.messages(self.target, "claude")
        self.messages(self.sender, "codex")
        return request


class ProjectConfirmationTests(ConfirmationFixture):
    """Withholding a message's contents until somebody has said yes."""

    def test_shell_send_and_confirmation_share_nested_context_lock(self):
        """The shell CLI holds and releases under the API's own lock."""
        script = str(pathlib.Path(bus.__file__).resolve())
        env = dict(os.environ, AGENTBUS_DIR=self.directory,
                   AGENTBUS_SESSION=self.sender.session)
        subprocess.run([sys.executable,
                        script,
                        "send",
                        "codex",
                        "claude",
                        self.marker],
                       env=env,
                       check=True,
                       capture_output=True,
                       text=True,
                       timeout=5)
        self.assertNotIn(self.marker, self.log())
        probe = self.messages(self.target, "claude")[0]
        env["AGENTBUS_SESSION"] = self.target.session
        result = subprocess.run(
            [sys.executable, script, "confirm", "claude", probe["id"], "yes"],
            env=env, check=True, capture_output=True, text=True, timeout=5)
        self.assertIn("Project confirmed", result.stdout)
        self.assertIn(self.marker, self.log())

    def test_details_withheld_from_log_reads_and_public_metadata_for_all_kinds(
            self):
        """No kind of message leaks its text to the log, a read or metadata."""
        for kind in ("message", "task", "result"):
            with self.subTest(kind=kind):
                kwargs = {"kind": kind}
                if kind != "message":
                    kwargs["task_id"] = bus.new_task_id()
                request = self.send(**kwargs)
                self.assertEqual(request["status"], "awaiting_confirmation")
                self.assertNotIn(self.marker, json.dumps(request))
                self.assertNotIn(self.marker, self.log())
                incoming = self.messages(self.target, "claude")
                self.assertEqual([item["kind"]
                                 for item in incoming], ["project_check"])
                self.assertEqual(
                    incoming[0]["confirmation_id"],
                    request["confirmation_id"])
                self.assertNotIn(self.marker, json.dumps(incoming))
                self.assertIn("project", incoming[0]["text"])
                held = self.pending(request)
                self.assertIn(self.marker, held.read_text())
                self.assertEqual(stat.S_IMODE(held.stat().st_mode), 0o600)

    def test_only_addressed_session_can_confirm(self):
        """A window the check was not addressed to cannot answer it."""
        other = self.window("other", "claude", "claude-other")
        request = self.send()
        for client, agent in ((other, "claude"), (self.sender, "codex"),
                              (self.target, "gemini")):
            with self.subTest(session=client.session, agent=agent):
                with self.assertRaisesRegex(ValueError, "another window"):
                    self.confirm(request, client=client, agent=agent)
                self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.confirm(request)["status"], "confirmed")

    def test_reading_project_question_does_not_release_details(self):
        """Reading the question is not consent, and releases nothing."""
        request = self.send()
        incoming = bus.receive_and_settle(self.target, "claude", limit=100)
        self.assertEqual([item["kind"]
                         for item in incoming], ["project_check"])
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.messages(self.target, "claude"), [])
        status = json.loads(self.pending(request).read_text())["status"]
        self.assertEqual(status, "awaiting_confirmation")

    def test_rejection_never_releases_even_if_later_confirmed_yes(self):
        """A no is final: a later yes on the same request releases nothing."""
        request = self.send()
        self.assertEqual(self.confirm(request, False)["status"], "rejected")
        self.assertEqual(self.confirm(request, True)["status"], "rejected")
        self.assertNotIn(self.marker, self.log())
        self.assertNotIn(
            self.marker,
            json.dumps(
                self.messages(
                    self.target,
                    "claude")))
        notices = self.messages(self.sender, "codex")
        self.assertEqual([item["status"] for item in notices], ["rejected"])
        self.assertNotIn(self.marker, json.dumps(notices))

    @mock.patch.object(project_confirmation.time, "time")
    def test_expired_request_never_releases_content(self, clock):
        """A timed-out request releases nothing, however it is answered.

        Args:
            clock (Mock): Stands in for the clock the expiry is read
                against. It runs at real time while the request is being
                made, and is only frozen past the deadline for the
                answer itself.
        """
        clock.side_effect = REAL_TIME
        request = self.send()
        expires = json.loads(self.pending(request).read_text())["expires_at"]

        clock.side_effect = None
        clock.return_value = expires + 1
        result = self.confirm(request)

        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reason"], "confirmation_timeout")
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.confirm(request)["status"], "expired")
        self.assertNotIn(self.marker, self.log())

    def test_yes_releases_once_and_repeat_remains_idempotent_after_consumption(
            self):
        """Yes delivers exactly once, and answering again changes nothing."""
        request = self.send()
        bus.receive_and_settle(self.target, "claude", limit=100)
        accepted = self.confirm(request)
        self.assertEqual(accepted["status"], "confirmed")
        self.assertEqual(accepted["id"], request["id"])
        incoming = bus.receive_and_settle(self.target, "claude", limit=100)
        self.assertEqual([item["text"] for item in incoming], [self.marker])
        for accept in (True, False):
            self.assertEqual(
                self.confirm(
                    request,
                    accept)["status"],
                "confirmed")
            self.assertEqual(self.messages(self.target, "claude"), [])
        notices = [item for item in self.messages(self.sender, "codex")
                   if item["kind"] == "project_status"]
        self.assertEqual(len(notices), 1)

    def test_confirmed_pair_sends_and_replies_without_more_questions(self):
        """An agreed pair talks both ways without being asked again."""
        first = self.establish()
        before = sum(
            item["kind"] == "project_check" for item in self.records())
        sent = self.send(text="next message")
        reply = bus.send(self.target, "claude", "codex", "reply to sender",
                         reply_to=first["id"], return_record=True)
        self.assertEqual(sent["status"], "queued")
        self.assertEqual(reply["status"], "queued")
        self.assertEqual(sent["confirmations"], [])
        self.assertEqual(reply["confirmations"], [])
        checks = sum(item["kind"] == "project_check"
                     for item in self.records())
        self.assertEqual(checks, before)
        self.assertEqual([item["text"] for item in self.messages(
            self.target, "claude")], ["next message"])
        self.assertEqual([item["text"] for item in self.messages(
            self.sender, "codex")], ["reply to sender"])


class ProjectConfirmationContextTests(ConfirmationFixture):
    """What survives an agreement, and what has to be asked again.

    A yes is granted to one pair of identities in one pair of contexts.
    These cover what happens when that stops being true -- a job change,
    a rename, a replacement session, a recycled pid -- along with
    broadcast fan-out, task and result carriage, and argument checking.
    """

    def test_sender_job_change_requires_new_confirmation(self):
        """The sender moving to another job invalidates the agreement."""
        self.establish()
        bus.set_job(self.sender, "different-project")
        request = self.send(text="new project details")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn("new project details", self.log())

    def test_recipient_job_change_requires_new_confirmation_immediately(self):
        """The recipient moving job invalidates it before any next send."""
        self.establish()
        bus.set_job(self.target, "different-project")
        request = self.send(text="recipient changed projects")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn("recipient changed projects", self.log())

    def test_task_change_on_either_side_requires_new_confirmation(self):
        """Renaming either window to another task invalidates the agreement."""
        for changed_side in ("sender", "recipient"):
            with self.subTest(changed_side=changed_side):
                self.establish()
                if changed_side == "sender":
                    client = self.sender
                else:
                    client = self.target
                agent = "codex" if changed_side == "sender" else "claude"
                original = bus.current_handle(client, agent)
                bus.set_name(client, agent + "-different")
                request = self.send(bus.current_handle(self.target, "claude"),
                                    text="changed task content")
                self.assertEqual(request["status"], "awaiting_confirmation")
                self.assertNotIn("changed task content", self.log())
                self.confirm(request, False)
                bus.set_name(client, original)

    def test_job_round_trip_on_either_side_requires_new_confirmation(self):
        """Leaving a job and returning does not revive the old agreement."""
        for changed_side in ("sender", "recipient"):
            with self.subTest(changed_side=changed_side):
                self.establish()
                if changed_side == "sender":
                    client = self.sender
                else:
                    client = self.target
                original = client.job
                bus.set_job(client, "temporary-project")
                bus.set_job(client, original)
                details = "job round trip requires a new yes: " + changed_side
                request = self.send(text=details)
                self.assertEqual(request["status"], "awaiting_confirmation")
                self.assertNotIn(details, self.log())

    def test_task_round_trip_on_either_side_requires_new_confirmation(self):
        """Leaving a task name and returning does not revive it either."""
        for changed_side in ("sender", "recipient"):
            with self.subTest(changed_side=changed_side):
                self.establish()
                if changed_side == "sender":
                    client = self.sender
                else:
                    client = self.target
                agent = "codex" if changed_side == "sender" else "claude"
                original = bus.current_handle(client, agent)
                bus.set_name(client, agent + "-temporary-task")
                bus.set_name(client, original)
                details = "task round trip requires a new yes: " + changed_side
                request = self.send(text=details)
                self.assertEqual(request["status"], "awaiting_confirmation")
                self.assertNotIn(details, self.log())

    def test_number_only_rename_keeps_confirmation(self):
        """Taking a different number on the same task is the same window."""
        self.establish()
        bus.set_name(self.sender, "codex-project-007")
        bus.set_name(self.target, "claude-project-008")
        request = self.send(text="same project after renumbering")
        self.assertEqual(request["status"], "queued")
        self.assertEqual([item["text"] for item in self.messages(
            self.target, "claude")], ["same project after renumbering"])

    def test_replacement_session_cannot_inherit_confirmed_handle(self):
        """A new session reusing a confirmed handle inherits no agreement."""
        self.establish()
        previous_handle = bus.current_handle(self.target, "claude")
        bus.set_name(self.target, "claude-previous")
        replacement = self.window("replacement", "claude", previous_handle)
        request = self.send(
            previous_handle,
            text="replacement needs confirmation")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn("replacement needs confirmation", self.log())
        self.assertEqual([item["kind"] for item in self.messages(
            replacement, "claude")], ["project_check"])

    def test_sender_context_change_expires_existing_check(self):
        """A check already in flight expires when its sender moves."""
        request = self.send()
        bus.set_name(self.sender, "codex-new-project")
        result = self.confirm(request)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reason"], "sender_context_changed")
        self.assertNotIn(self.marker, self.log())

    def test_absent_handle_only_gets_question_until_future_session_confirms(
            self):
        """Mail for an unregistered handle waits as a question, not as text."""
        request = self.send("claude-future-001")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.messages(self.target, "claude"), [])
        future = self.window("future", "claude", "claude-future-001")
        self.assertEqual([item["kind"] for item in self.messages(
            future, "claude")], ["project_check"])
        self.confirm(request, client=future)
        incoming = self.messages(future, "claude")
        self.assertEqual([item["text"] for item in incoming], [self.marker])
        self.assertEqual(incoming[0]["to_session"], future.session)
        self.assertEqual(self.messages(self.target, "claude"), [])

    def test_broadcast_recipients_confirm_separately_excluding_latecomers(
            self):
        """Each recipient answers for itself; latecomers get nothing."""
        other = self.window("other", "claude", "claude-other", "other-project")
        request = self.send(broadcast=True)
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn(self.marker, json.dumps(request))
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(len(request["confirmations"]), 2)
        requests = {item["to"]: item for item in request["confirmations"]}
        target_request = requests[bus.current_handle(self.target, "claude")]
        other_request = requests[bus.current_handle(other, "claude")]
        self.messages(self.target, "claude")
        self.messages(other, "claude")
        future = self.window("future", "claude", "claude-future")
        self.confirm(target_request)
        self.assertEqual([item["text"] for item in self.messages(
            self.target, "claude")], [self.marker])
        self.assertEqual(self.messages(other, "claude"), [])
        self.assertEqual(self.messages(future, "claude"), [])
        self.confirm(other_request, client=other)
        self.assertEqual(
            [item["text"] for item in self.messages(other, "claude")],
            [self.marker])
        self.assertEqual(self.messages(self.target, "claude"), [])
        self.assertEqual(self.messages(future, "claude"), [])

    def test_confirmed_task_and_result_retain_task_id_without_another_check(
            self):
        """A confirmed pair's task and result keep their id, unasked."""
        task_id = bus.new_task_id()
        request = self.send(kind="task", task_id=task_id)
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.messages(self.target, "claude")
        self.confirm(request)
        task = self.messages(self.target, "claude")[0]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["task_id"], task_id)
        self.messages(self.sender, "codex")
        result = bus.send(
            self.target,
            "claude",
            "codex",
            "task completed",
            kind="result",
            task_id=task_id,
            reply_to=request["id"],
            return_record=True)
        self.assertEqual(result["status"], "queued")
        received = self.messages(self.sender, "codex")
        self.assertEqual([item["kind"] for item in received], ["result"])
        self.assertEqual(received[0]["task_id"], task_id)

    def test_reused_process_id_cannot_inherit_confirmation_or_pending_check(
            self):
        """A recycled pid is a different window, and inherits nothing."""
        with fixtures.process_generation("1000"):
            legacy = self.window("claude200", "claude", "claude-legacy-001")
            handle = bus.current_handle(legacy, "claude")
            unanswered = self.send(handle, text="unanswered original payload")
            approved = self.send(handle, text="approved original payload")
            self.assertEqual(
                self.confirm(
                    approved,
                    client=legacy)["status"],
                "confirmed")
        with fixtures.process_generation("2000"):
            replacement = self.window("claude200", "claude", handle)
            self.assertEqual(self.messages(replacement, "claude"), [])
            with self.assertRaises(ValueError):
                self.confirm(unanswered, client=replacement)
            fresh = self.send(handle, text=self.marker)
            self.assertEqual(fresh["status"], "awaiting_confirmation")
            self.assertNotIn(self.marker, self.log())
            self.assertEqual([item["kind"] for item in self.messages(
                replacement, "claude")], ["project_check"])

    def test_pending_source_does_not_alias_a_replacement_process_conversation(
            self):
        """A held request does not resolve onto whoever took its pid next."""
        with fixtures.process_generation("1000"):
            legacy = self.window("codex200", "codex", "codex-source-001")
            task_id = bus.new_task_id()
            request = bus.send(
                legacy,
                "codex",
                bus.current_handle(
                    self.target,
                    "claude"),
                self.marker,
                kind="task",
                task_id=task_id,
                return_record=True)
            self.assertEqual(bus.get_task(legacy, task_id)[
                             "sender_session_start"], "1000")
        replacement = bus.connect(
            self.directory,
            session="replacement-conversation",
            cwd=legacy.cwd)
        with fixtures.process_identity(pid=200, agent="codex",
                                       generation="2000"):
            bus.bind_session(replacement, "codex")
            bus.register(replacement, "codex")
            result = self.confirm(request)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reason"], "sender_context_changed")
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(bus.get_task(legacy, task_id)["status"], "expired")

    CACHED_TEXT = "cached message paused before append"

    def setUp(self):
        """Arrange the handoff the concurrency test choreographs."""
        super().setUp()
        self.append_reached = threading.Event()
        self.allow_append = threading.Event()
        self.change_started = threading.Event()
        self.change_finished = threading.Event()
        self.real_append = storage.append

    def pause_cached_append(self, client, record):
        """Stall the cached send inside append until the test lets go.

        Standing in for storage.append, this is what holds open the
        window the test then tries to slip a job change into.

        Args:
            client (Bus): Connection doing the append.
            record (dict): The message being appended.

        Returns:
            dict: Whatever the real append returned.

        Raises:
            TimeoutError: The test never released the pause.
        """
        cached = (record.get("kind") == "message"
                  and record.get("text") == self.CACHED_TEXT)
        if cached:
            self.append_reached.set()
            if not self.allow_append.wait(timeout=3):
                raise TimeoutError("test did not release the cached append")
        return self.real_append(client, record)

    def send_cached(self):
        """Send the message that will stall inside append.

        Returns:
            dict: The queued record.
        """
        return self.send(text=self.CACHED_TEXT)

    def change_project(self):
        """Move the target to another job while the sender is stalled."""
        self.change_started.set()
        try:
            bus.set_job(self.target, "project-changed-during-send")
        finally:
            self.change_finished.set()

    @mock.patch.object(storage, "append")
    def test_cached_send_and_job_change_share_one_atomic_context_boundary(
            self, append):
        """A job change cannot slip between authorization and the append.

        Args:
            append (Mock): Stands in for the append, stalling only the
                one cached message so the test can try to change the job
                while that send is mid-flight.
        """
        append.side_effect = self.pause_cached_append
        self.establish()

        with futures.ThreadPoolExecutor(max_workers=2) as pool:
            sending = pool.submit(self.send_cached)
            changing = None
            try:
                self.assertTrue(self.append_reached.wait(timeout=3))
                changing = pool.submit(self.change_project)
                self.assertTrue(self.change_started.wait(timeout=3))
                self.assertFalse(
                    self.change_finished.wait(timeout=0.1),
                    "job changed after authorization but before append")
            finally:
                self.allow_append.set()

            # A future re-raises in this thread, so a failure inside
            # either worker surfaces as this test failing rather than as
            # a thread dying quietly.
            queued = sending.result(timeout=3)
            if changing is not None:
                changing.result(timeout=3)

        self.assertTrue(self.change_finished.is_set())
        self.assertEqual(queued["status"], "queued")
        self.assertIn(self.CACHED_TEXT, self.log())
        following = self.send(text="new context needs fresh consent")
        self.assertEqual(following["status"], "awaiting_confirmation")
        self.assertNotIn("new context needs fresh consent", self.log())

    def test_confirmation_requires_boolean_acceptance(self):
        """Anything but a real True or False is refused, releasing nothing."""
        request = self.send()
        for value in ("yes", "no", "true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "explicit yes or no"):
                    self.confirm(request, value)
                self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.confirm(request)["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
