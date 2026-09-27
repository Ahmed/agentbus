"""agentbus_identity.py regressions."""

import json
import os
import unittest
import unittest.mock as mock

import agentbus_identity as identity
import bus
from tests import agentbus_test_utils as test_utils


class IdentityTest(test_utils.BusFixture):
    """Regressions for identity resolution and inheritance."""

    TEMP_PREFIX = "agentbus-identity-test-"

    def test_codex_helper_inherits_parent_context(self):
        """A sub-agent inherits its parent's roster name and confirmations."""
        # 1. A parent session is created.
        parent = self.window("parent-session", "codex", "fix-bug")

        # 2. A helper thread appears.
        helper_thread_id = "thread-helper"
        rollout_dir = os.path.join(
            self.directory, ".codex/sessions/2020/01/01")
        os.makedirs(rollout_dir)
        rollout_file = os.path.join(
            rollout_dir, f"rollout-12345-{helper_thread_id}.jsonl"
        )
        with open(rollout_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "payload": {
                        "parent_thread_id": parent.session,
                    }
                },
                f,
            )

        with mock.patch.dict(
            os.environ, {"CODEX_THREAD_ID": helper_thread_id}
        ), mock.patch("os.path.expanduser", return_value=self.directory):
            # 3. The helper connects and inherits the parent's context.
            helper = self.window(helper_thread_id, "codex", "fix-bug-helper")
            self.assertEqual(
                identity.get_parent_session(helper),
                parent.session)

            # 4. The helper's name on the roster reflects its parent.
            roster = self.get_roster_for_session(helper)
            helper_roster_entry = next(
                (r for r in roster if r["session"] == helper.session), None
            )
            self.assertIsNotNone(helper_roster_entry)
            self.assertEqual(helper_roster_entry.get("parent"), parent.session)
            self.assertIn(
                f"(helper of {parent.handle})",
                helper_roster_entry.get("display_handle"),
            )

    def get_roster_for_session(self, client):
        """Helper to get the full roster from the perspective of a client."""
        return bus.agents(client)


if __name__ == "__main__":
    unittest.main()
