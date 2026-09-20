"""MCP project-confirmation workflows using temporary buses only."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import agentbus_server as server
import bus
import agentbus_identity as identity
import agentbus_messages as messaging
import agentbus_routing as routing
import project_confirmation


class MCPConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agentbus-mcp-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        environment = mock.patch.dict(os.environ, {"AGENTBUS_JOB": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.requester = self.window("requester", "codex", "codex-mcp-project")
        self.assignee = self.window("assignee", "claude", "claude-mcp-project")

    def window(self, session, agent, handle, job="mcp-project"):
        client = bus.connect(self.directory, session=session,
                             cwd=os.path.join(self.directory, session))
        bus.set_job(client, job)
        bus.register(client, agent)
        bus.set_name(client, handle)
        return client

    async def call(self, client, agent, tool, *args, **kwargs):
        with mock.patch.object(server, "_client", return_value=client), \
                mock.patch.object(server, "_agent_name", return_value=agent):
            return await tool(*args, **kwargs)

    def records(self):
        return [json.loads(line) for line in Path(self.requester.path).read_text().splitlines()
                if line.strip()]

    def checks_for(self, client, agent):
        return [record for record in bus.peek(client, agent)
                if record["kind"] == "project_check"]

    async def delegate(self, text="Private task contents", task_id="task_mcp_1"):
        with mock.patch.object(bus, "new_task_id", return_value=task_id):
            response = await self.call(self.requester, "codex", server.delegate_task,
                                       "claude", text)
        return task_id, response

    async def accept_task(self, task_id):
        checks = self.checks_for(self.assignee, "claude")
        self.assertEqual(len(checks), 1)
        check = checks[0]
        response = await self.call(self.assignee, "claude", server.confirm_project,
                                   check["confirmation_id"], True)
        self.assertIn("confirmed", response)
        response = await self.call(self.assignee, "claude", server.receive_messages)
        self.assertIn(task_id, response)
        self.assertEqual(bus.get_task(self.requester, task_id)["status"], "delivered")
        return check

    async def test_delegate_confirm_receive_and_result_reuse_project_relationship(self):
        task_text = "Private task: investigate the unique extraction defect."
        task_id, response = await self.delegate(task_text)
        task = bus.get_task(self.requester, task_id)
        self.assertEqual(task["status"], "awaiting_confirmation")
        self.assertEqual(task["sender_session"], self.requester.session)
        self.assertEqual(task["assignee_session"], self.assignee.session)
        self.assertEqual(task["assignee_agent"], "claude")
        self.assertIn("Awaiting project confirmation", response)
        self.assertNotIn(task_text, response)
        self.assertNotIn(task_text, Path(self.requester.path).read_text())
        self.assertEqual([record["kind"] for record in self.records()], ["project_check"])
        self.assertIn(self.checks_for(self.assignee, "claude")[0]["confirmation_id"], response)

        await self.accept_task(task_id)
        self.assertIn(task_text, Path(self.requester.path).read_text())
        checks_before = sum(record["kind"] == "project_check" for record in self.records())
        result_text = "Private result: the extraction defect has been repaired."
        response = await self.call(self.assignee, "claude", server.report_result,
                                   task_id, result_text)
        self.assertIn("returned to " + bus.current_handle(self.requester, "codex"), response)
        self.assertEqual(bus.get_task(self.requester, task_id)["status"], "done")
        self.assertEqual(sum(record["kind"] == "project_check" for record in self.records()),
                         checks_before)
        response = await self.call(self.requester, "codex", server.receive_messages)
        self.assertIn(result_text, response)

    async def test_changed_context_holds_result_until_requester_confirms(self):
        task_id, _ = await self.delegate()
        await self.accept_task(task_id)
        original_handle = bus.current_handle(self.requester, "codex")
        bus.set_name(self.requester, "codex-mcp-followup")
        replacement = self.window("replacement", "codex", original_handle)
        result_text = "Held result: details belong to the original requester."
        response = await self.call(self.assignee, "claude", server.report_result,
                                   task_id, result_text)
        self.assertIn("Awaiting project confirmation", response)
        self.assertNotIn(result_text, response)
        self.assertNotIn(result_text, Path(self.requester.path).read_text())
        task = bus.get_task(self.requester, task_id)
        self.assertEqual(task["status"], "delivered")
        self.assertEqual(task["result_confirmation_status"], "awaiting_confirmation")
        self.assertEqual(bus.peek(replacement, "codex"), [])
        checks = self.checks_for(self.requester, "codex")
        self.assertEqual(len(checks), 1)
        self.assertIn(checks[0]["confirmation_id"], response)
        self.assertEqual(checks[0]["to_session"], self.requester.session)

        response = await self.call(self.requester, "codex", server.confirm_project,
                                   checks[0]["confirmation_id"], True)
        self.assertIn("confirmed", response)
        self.assertEqual(bus.get_task(self.requester, task_id)["status"], "done")
        response = await self.call(self.requester, "codex", server.receive_messages)
        self.assertIn(result_text, response)
        self.assertEqual(bus.peek(replacement, "codex"), [])

    async def test_task_ledger_routes_result_when_original_message_is_missing(self):
        task_id, _ = await self.delegate()
        await self.accept_task(task_id)
        task = bus.get_task(self.requester, task_id)
        original_handle = bus.current_handle(self.requester, "codex")
        bus.set_name(self.requester, "codex-after-compaction")
        replacement = self.window("replacement", "codex", original_handle)
        find_message = routing.find_message

        def compacted_log(client, message_id):
            if message_id == task["message_id"]:
                return None
            return find_message(client, message_id)

        with mock.patch.object(routing, "find_message", side_effect=compacted_log):
            response = await self.call(self.assignee, "claude", server.report_result,
                                       task_id, "Result after original message compaction")
        self.assertIn("Awaiting project confirmation", response)
        checks = self.checks_for(self.requester, "codex")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["to_session"], self.requester.session)
        self.assertEqual(bus.peek(replacement, "codex"), [])

    async def test_fast_confirmation_and_receive_do_not_lose_delivered_status(self):
        send = server._send

        def send_then_accept_and_receive(*args, **kwargs):
            record = send(*args, **kwargs)
            project_confirmation.confirm_project(self.assignee, "claude",
                                                 record["confirmation_id"], True)
            bus.receive_and_settle(self.assignee, "claude")
            return record

        with mock.patch.object(server, "_send", side_effect=send_then_accept_and_receive):
            task_id, _ = await self.delegate()
        self.assertEqual(bus.get_task(self.requester, task_id)["status"], "delivered")

    async def test_mixed_broadcast_reports_only_checks_still_waiting(self):
        task_id, _ = await self.delegate()
        await self.accept_task(task_id)
        unconfirmed = self.window("other", "claude", "claude-another-project", "other")
        payload = "Broadcast contents kept from the unconfirmed window."
        response = await self.call(self.requester, "codex", server.broadcast_message,
                                   payload, to="claude")
        check = self.checks_for(unconfirmed, "claude")[0]
        self.assertIn(check["confirmation_id"], response)
        self.assertNotIn("check None", response)
        self.assertNotIn(bus.current_handle(self.assignee, "claude"), response)
        self.assertNotIn(payload, response)
        self.assertNotIn(payload, [record["text"] for record in bus.peek(unconfirmed, "claude")])
        self.assertIn(payload, [record["text"] for record in bus.peek(self.assignee, "claude")])


if __name__ == "__main__":
    unittest.main()
