"""Project-confirmation regressions for helper processes."""

import json
import os
import unittest
import unittest.mock as mock

import agentbus_identity as identity
import bus
from tests.test_project_confirmation import ConfirmationFixture


class ProjectConfirmationHelperTests(ConfirmationFixture):
    """Tests for project confirmation involving helper processes."""

    def setUp(self):
        """Set up the test fixture."""
        super().setUp()
        self.parent_session_id = "parent-session"
        self.helper_thread_id = "thread-helper"
        self.target_session_id = "target-session"

        # Create a mock rollout file for the helper
        rollout_dir = os.path.join(
            self.directory, ".codex", "sessions", "2020", "01", "01"
        )
        os.makedirs(rollout_dir, exist_ok=True)
        rollout_file = os.path.join(
            rollout_dir, f"rollout-12345-{self.helper_thread_id}.jsonl"
        )
        with open(rollout_file, "w", encoding="utf-8") as f:
            json.dump(
                {"payload": {"parent_thread_id": self.parent_session_id}}, f
            )

    def test_helper_inherits_parent_confirmation(self):
        """A helper whose parent is confirmed sends without a check."""
        parent = self.window(self.parent_session_id, "codex", "codex-project")
        target = self.window(
            self.target_session_id,
            "claude",
            "claude-project")
        marker = "HELD_PAYLOAD_FOR_PARENT"

        # 1. Establish confirmation between parent and target
        request = bus.send(
            parent, "codex", target.handle, marker, return_record=True
        )
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.confirm(request, client=target, agent="claude")
        self.assertIn(marker, self.log())
        # Clear messages
        self.messages(target, "claude")
        self.messages(parent, "codex")

        env = {
            "CODEX_THREAD_ID": self.helper_thread_id,
            "AGENTBUS_DIR": self.directory,
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "os.path.expanduser", return_value=self.directory
        ):
            helper = self.window(
                self.helper_thread_id,
                "codex",
                "fix-bug-helper")
            self.assertEqual(
                identity.get_parent_session(helper),
                parent.session)

            # 2. Helper sends a message to the target
            helper_message = "Message from helper"
            request = bus.send(
                helper,
                "codex",
                target.handle,
                helper_message,
                return_record=True)

            # 3. Assert message is sent directly
            self.assertEqual(request["status"], "queued")
            self.assertEqual(request.get("confirmations"), [])

            # 4. Verify target receives the message
            received_messages = self.messages(target, "claude")
            self.assertEqual(len(received_messages), 1)
            self.assertEqual(received_messages[0]["text"], helper_message)

    def test_helper_without_confirmed_parent_gets_check(self):
        """A helper whose parent is not confirmed gets a project check."""
        parent = self.window(self.parent_session_id, "codex", "codex-project")
        target = self.window(
            self.target_session_id,
            "claude",
            "claude-project")

        env = {
            "CODEX_THREAD_ID": self.helper_thread_id,
            "AGENTBUS_DIR": self.directory,
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "os.path.expanduser", return_value=self.directory
        ):
            helper = self.window(
                self.helper_thread_id,
                "codex",
                "fix-bug-helper")
            self.assertEqual(
                identity.get_parent_session(helper),
                parent.session)

            # 2. Helper sends a message to the target
            helper_message = "Message from helper"
            request = bus.send(
                helper,
                "codex",
                target.handle,
                helper_message,
                return_record=True)

            # 3. Assert message is awaiting confirmation
            self.assertEqual(request["status"], "awaiting_confirmation")
            self.assertIn(
                "confirmation_id", request.get(
                    "confirmations", [
                        {}])[0])

            # 4. Verify target receives a project_check
            received_messages = self.messages(target, "claude")
            self.assertEqual(len(received_messages), 1)
            self.assertEqual(received_messages[0]["kind"], "project_check")
            self.assertNotIn(helper_message, self.log())


if __name__ == "__main__":
    unittest.main()
