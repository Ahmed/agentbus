"""Project-confirmation regressions using real sends on temporary buses."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import bus
import agentbus_storage as storage
import agentbus_identity as identity
import project_confirmation


class ProjectConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agentbus-confirmation-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        environment = mock.patch.dict(os.environ, {"AGENTBUS_JOB": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.sender = self.window("sender", "codex", "codex-project", "project")
        self.target = self.window("target", "claude", "claude-project", "project")
        self.marker = "HELD_PAYLOAD_MUST_NOT_BE_DISCLOSED"

    def window(self, session, agent, handle, job="project"):
        client = bus.connect(self.directory, session=session,
                             cwd=os.path.join(self.directory, session))
        bus.set_job(client, job)
        bus.register(client, agent)
        bus.set_name(client, handle)
        return client

    def send(self, to="claude", text=None, **kwargs):
        return bus.send(self.sender, "codex", to, text or self.marker,
                        return_record=True, **kwargs)

    def confirm(self, record, accept=True, client=None, agent="claude"):
        return project_confirmation.confirm_project(
            client or self.target, agent, record["confirmation_id"], accept)

    def messages(self, client, agent):
        return bus.receive(client, agent, limit=100)

    def log(self):
        return Path(self.sender.path).read_text()

    def records(self):
        return [json.loads(line) for line in self.log().splitlines()]

    def pending(self, record):
        return Path(self.sender.state) / ("pending.%s.json" % record["confirmation_id"])

    def establish(self):
        request = self.send()
        self.assertEqual(self.confirm(request)["status"], "confirmed")
        self.messages(self.target, "claude")
        self.messages(self.sender, "codex")
        return request

    def test_shell_send_and_confirmation_share_nested_context_lock(self):
        script = str(Path(bus.__file__).resolve())
        env = dict(os.environ, AGENTBUS_DIR=self.directory,
                   AGENTBUS_SESSION=self.sender.session)
        subprocess.run([sys.executable, script, "send", "codex", "claude", self.marker],
                       env=env, check=True, capture_output=True, text=True, timeout=5)
        self.assertNotIn(self.marker, self.log())
        probe = self.messages(self.target, "claude")[0]
        env["AGENTBUS_SESSION"] = self.target.session
        result = subprocess.run(
            [sys.executable, script, "confirm", "claude", probe["id"], "yes"],
            env=env, check=True, capture_output=True, text=True, timeout=5)
        self.assertIn("Project confirmed", result.stdout)
        self.assertIn(self.marker, self.log())

    def test_details_withheld_from_log_reads_and_public_metadata_for_all_kinds(self):
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
                self.assertEqual([item["kind"] for item in incoming], ["project_check"])
                self.assertEqual(incoming[0]["confirmation_id"], request["confirmation_id"])
                self.assertNotIn(self.marker, json.dumps(incoming))
                self.assertIn("project", incoming[0]["text"])
                held = self.pending(request)
                self.assertIn(self.marker, held.read_text())
                self.assertEqual(stat.S_IMODE(held.stat().st_mode), 0o600)

    def test_only_addressed_session_can_confirm(self):
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
        request = self.send()
        incoming = bus.receive_and_settle(self.target, "claude", limit=100)
        self.assertEqual([item["kind"] for item in incoming], ["project_check"])
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.messages(self.target, "claude"), [])
        status = json.loads(self.pending(request).read_text())["status"]
        self.assertEqual(status, "awaiting_confirmation")

    def test_rejection_never_releases_even_if_later_confirmed_yes(self):
        request = self.send()
        self.assertEqual(self.confirm(request, False)["status"], "rejected")
        self.assertEqual(self.confirm(request, True)["status"], "rejected")
        self.assertNotIn(self.marker, self.log())
        self.assertNotIn(self.marker, json.dumps(self.messages(self.target, "claude")))
        notices = self.messages(self.sender, "codex")
        self.assertEqual([item["status"] for item in notices], ["rejected"])
        self.assertNotIn(self.marker, json.dumps(notices))

    def test_expired_request_never_releases_content(self):
        request = self.send()
        expires = json.loads(self.pending(request).read_text())["expires_at"]
        with mock.patch.object(project_confirmation.time, "time", return_value=expires + 1):
            result = self.confirm(request)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reason"], "confirmation_timeout")
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.confirm(request)["status"], "expired")
        self.assertNotIn(self.marker, self.log())

    def test_yes_releases_once_and_repeat_remains_idempotent_after_consumption(self):
        request = self.send()
        bus.receive_and_settle(self.target, "claude", limit=100)
        accepted = self.confirm(request)
        self.assertEqual(accepted["status"], "confirmed")
        self.assertEqual(accepted["id"], request["id"])
        incoming = bus.receive_and_settle(self.target, "claude", limit=100)
        self.assertEqual([item["text"] for item in incoming], [self.marker])
        for accept in (True, False):
            self.assertEqual(self.confirm(request, accept)["status"], "confirmed")
            self.assertEqual(self.messages(self.target, "claude"), [])
        notices = [item for item in self.messages(self.sender, "codex")
                   if item["kind"] == "project_status"]
        self.assertEqual(len(notices), 1)

    def test_confirmed_pair_sends_and_replies_without_more_questions(self):
        first = self.establish()
        before = sum(item["kind"] == "project_check" for item in self.records())
        sent = self.send(text="next message")
        reply = bus.send(self.target, "claude", "codex", "reply to sender",
                         reply_to=first["id"], return_record=True)
        self.assertEqual(sent["status"], "queued")
        self.assertEqual(reply["status"], "queued")
        self.assertEqual(sent["confirmations"], [])
        self.assertEqual(reply["confirmations"], [])
        self.assertEqual(sum(item["kind"] == "project_check" for item in self.records()), before)
        self.assertEqual([item["text"] for item in self.messages(self.target, "claude")],
                         ["next message"])
        self.assertEqual([item["text"] for item in self.messages(self.sender, "codex")],
                         ["reply to sender"])

    def test_sender_job_change_requires_new_confirmation(self):
        self.establish()
        bus.set_job(self.sender, "different-project")
        request = self.send(text="new project details")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn("new project details", self.log())

    def test_recipient_job_change_requires_new_confirmation_immediately(self):
        self.establish()
        bus.set_job(self.target, "different-project")
        request = self.send(text="recipient changed projects")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn("recipient changed projects", self.log())

    def test_task_change_on_either_side_requires_new_confirmation(self):
        for changed_side in ("sender", "recipient"):
            with self.subTest(changed_side=changed_side):
                self.establish()
                client = self.sender if changed_side == "sender" else self.target
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
        for changed_side in ("sender", "recipient"):
            with self.subTest(changed_side=changed_side):
                self.establish()
                client = self.sender if changed_side == "sender" else self.target
                original = client.job
                bus.set_job(client, "temporary-project")
                bus.set_job(client, original)
                details = "job round trip requires a new yes: " + changed_side
                request = self.send(text=details)
                self.assertEqual(request["status"], "awaiting_confirmation")
                self.assertNotIn(details, self.log())

    def test_task_round_trip_on_either_side_requires_new_confirmation(self):
        for changed_side in ("sender", "recipient"):
            with self.subTest(changed_side=changed_side):
                self.establish()
                client = self.sender if changed_side == "sender" else self.target
                agent = "codex" if changed_side == "sender" else "claude"
                original = bus.current_handle(client, agent)
                bus.set_name(client, agent + "-temporary-task")
                bus.set_name(client, original)
                details = "task round trip requires a new yes: " + changed_side
                request = self.send(text=details)
                self.assertEqual(request["status"], "awaiting_confirmation")
                self.assertNotIn(details, self.log())

    def test_number_only_rename_keeps_confirmation(self):
        self.establish()
        bus.set_name(self.sender, "codex-project-007")
        bus.set_name(self.target, "claude-project-008")
        request = self.send(text="same project after renumbering")
        self.assertEqual(request["status"], "queued")
        self.assertEqual([item["text"] for item in self.messages(self.target, "claude")],
                         ["same project after renumbering"])

    def test_replacement_session_cannot_inherit_confirmed_handle(self):
        self.establish()
        previous_handle = bus.current_handle(self.target, "claude")
        bus.set_name(self.target, "claude-previous")
        replacement = self.window("replacement", "claude", previous_handle)
        request = self.send(previous_handle, text="replacement needs confirmation")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn("replacement needs confirmation", self.log())
        self.assertEqual([item["kind"] for item in self.messages(replacement, "claude")],
                         ["project_check"])

    def test_sender_context_change_expires_existing_check(self):
        request = self.send()
        bus.set_name(self.sender, "codex-new-project")
        result = self.confirm(request)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reason"], "sender_context_changed")
        self.assertNotIn(self.marker, self.log())

    def test_absent_handle_only_gets_question_until_future_session_confirms(self):
        request = self.send("claude-future-001")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.messages(self.target, "claude"), [])
        future = self.window("future", "claude", "claude-future-001")
        self.assertEqual([item["kind"] for item in self.messages(future, "claude")],
                         ["project_check"])
        self.confirm(request, client=future)
        incoming = self.messages(future, "claude")
        self.assertEqual([item["text"] for item in incoming], [self.marker])
        self.assertEqual(incoming[0]["to_session"], future.session)
        self.assertEqual(self.messages(self.target, "claude"), [])

    def test_broadcast_each_recipient_confirms_separately_and_future_windows_are_excluded(self):
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
        self.assertEqual([item["text"] for item in self.messages(self.target, "claude")],
                         [self.marker])
        self.assertEqual(self.messages(other, "claude"), [])
        self.assertEqual(self.messages(future, "claude"), [])
        self.confirm(other_request, client=other)
        self.assertEqual([item["text"] for item in self.messages(other, "claude")],
                         [self.marker])
        self.assertEqual(self.messages(self.target, "claude"), [])
        self.assertEqual(self.messages(future, "claude"), [])

    def test_confirmed_task_and_result_retain_task_id_without_another_check(self):
        task_id = bus.new_task_id()
        request = self.send(kind="task", task_id=task_id)
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.messages(self.target, "claude")
        self.confirm(request)
        task = self.messages(self.target, "claude")[0]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["task_id"], task_id)
        self.messages(self.sender, "codex")
        result = bus.send(self.target, "claude", "codex", "task completed",
                          kind="result", task_id=task_id, reply_to=request["id"],
                          return_record=True)
        self.assertEqual(result["status"], "queued")
        received = self.messages(self.sender, "codex")
        self.assertEqual([item["kind"] for item in received], ["result"])
        self.assertEqual(received[0]["task_id"], task_id)

    def test_reused_process_id_cannot_inherit_confirmation_or_pending_check(self):
        with mock.patch.object(identity, "_process_start", return_value="1000"):
            legacy = self.window("claude200", "claude", "claude-legacy-001")
            handle = bus.current_handle(legacy, "claude")
            unanswered = self.send(handle, text="unanswered original payload")
            approved = self.send(handle, text="approved original payload")
            self.assertEqual(self.confirm(approved, client=legacy)["status"], "confirmed")
        with mock.patch.object(identity, "_process_start", return_value="2000"):
            replacement = self.window("claude200", "claude", handle)
            self.assertEqual(self.messages(replacement, "claude"), [])
            with self.assertRaises(ValueError):
                self.confirm(unanswered, client=replacement)
            fresh = self.send(handle, text=self.marker)
            self.assertEqual(fresh["status"], "awaiting_confirmation")
            self.assertNotIn(self.marker, self.log())
            self.assertEqual([item["kind"] for item in self.messages(replacement, "claude")],
                             ["project_check"])

    def test_pending_source_does_not_alias_a_replacement_process_conversation(self):
        with mock.patch.object(identity, "_process_start", return_value="1000"):
            legacy = self.window("codex200", "codex", "codex-source-001")
            task_id = bus.new_task_id()
            request = bus.send(legacy, "codex", bus.current_handle(self.target, "claude"),
                               self.marker, kind="task", task_id=task_id,
                               return_record=True)
            self.assertEqual(bus.get_task(legacy, task_id)["sender_session_start"], "1000")
        replacement = bus.connect(self.directory, session="replacement-conversation",
                                  cwd=legacy.cwd)
        with mock.patch.object(identity, "session_pid", return_value=200), \
                mock.patch.object(identity, "_process_name", return_value="codex"), \
                mock.patch.object(identity, "_process_start", return_value="2000"):
            bus.bind_session(replacement, "codex")
            bus.register(replacement, "codex")
            result = self.confirm(request)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reason"], "sender_context_changed")
        self.assertNotIn(self.marker, self.log())
        self.assertEqual(bus.get_task(legacy, task_id)["status"], "expired")

    def test_cached_send_and_job_change_share_one_atomic_context_boundary(self):
        self.establish()
        append_reached = threading.Event()
        allow_append = threading.Event()
        change_started = threading.Event()
        change_finished = threading.Event()
        outcomes = {}
        append = storage.append
        cached_text = "cached message paused before append"

        def pause_cached_append(client, record):
            if record.get("kind") == "message" and record.get("text") == cached_text:
                append_reached.set()
                if not allow_append.wait(timeout=3):
                    raise TimeoutError("test did not release the cached append")
            return append(client, record)

        def send_cached():
            try:
                outcomes["send"] = self.send(text=cached_text)
            except Exception as error:
                outcomes["send_error"] = error

        def change_project():
            change_started.set()
            try:
                bus.set_job(self.target, "project-changed-during-send")
            except Exception as error:
                outcomes["change_error"] = error
            finally:
                change_finished.set()

        sending = threading.Thread(target=send_cached, daemon=True)
        changing = threading.Thread(target=change_project, daemon=True)
        with mock.patch.object(storage, "append", side_effect=pause_cached_append):
            sending.start()
            try:
                self.assertTrue(append_reached.wait(timeout=3))
                changing.start()
                self.assertTrue(change_started.wait(timeout=3))
                self.assertFalse(change_finished.wait(timeout=0.1),
                                 "job changed after authorization but before payload append")
            finally:
                allow_append.set()
                sending.join(timeout=3)
                if changing.ident is not None:
                    changing.join(timeout=3)
        self.assertFalse(sending.is_alive())
        self.assertFalse(changing.is_alive())
        self.assertNotIn("send_error", outcomes)
        self.assertNotIn("change_error", outcomes)
        self.assertTrue(change_finished.is_set())
        self.assertEqual(outcomes["send"]["status"], "queued")
        self.assertIn(cached_text, self.log())
        following = self.send(text="new context needs fresh consent")
        self.assertEqual(following["status"], "awaiting_confirmation")
        self.assertNotIn("new context needs fresh consent", self.log())

    def test_confirmation_requires_boolean_acceptance(self):
        request = self.send()
        for value in ("yes", "no", "true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "explicit yes or no"):
                    self.confirm(request, value)
                self.assertNotIn(self.marker, self.log())
        self.assertEqual(self.confirm(request)["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
