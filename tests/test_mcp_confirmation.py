"""MCP project-confirmation workflows using temporary buses only."""

import json
import pathlib
import unittest
import unittest.mock as mock

import agentbus_routing as routing
import agentbus_server as server
import bus
import project_confirmation
import tests.agentbus_test_utils as fixtures


class MCPConfirmationTests(fixtures.BusFixture,
                           unittest.IsolatedAsyncioTestCase):
    """The MCP tools hold a payload until the addressed window agrees.

    Mixes the shared bus fixture into the asyncio runner, because every
    tool here is a coroutine and the fixture is the same one the
    synchronous confirmation regressions use.
    """

    TEMP_PREFIX = "agentbus-mcp-test-"
    DEFAULT_JOB = "mcp-project"

    def __init__(self, *args, **kwargs):
        """Remember the real send before any test replaces it."""
        super().__init__(*args, **kwargs)
        self.real_send = bus.send

    def setUp(self):
        """Open the requester and assignee the tools talk between."""
        super().setUp()
        self.requester = self.window("requester", "codex", "codex-mcp-project")
        self.assignee = self.window("assignee", "claude", "claude-mcp-project")
        self.compacted_message_id = None
        self.real_find_message = routing.find_message

    def compacted_log(self, client, message_id):
        """Stand in for find_message with one message swept away.

        Compaction is what makes this interesting: once the original
        message is gone from the log, the result has nothing to reply to
        and the task ledger is the only thing left that can route it.

        Args:
            client (Bus): Connection searching the log.
            message_id (str): The id being looked for.

        Returns:
            dict or None: The record, or None for the compacted one.
        """
        if message_id == self.compacted_message_id:
            return None
        return self.real_find_message(client, message_id)

    async def call(self, client, agent, tool, *args, **kwargs):
        """Invoke one MCP tool as a chosen window.

        Args:
            client (Bus): Connection the tool should see as its own.
            agent (str): CLI name the tool should answer as.
            tool (coroutine function): The MCP tool to call.
            *args (object): Positional arguments for the tool.
            **kwargs (object): Keyword arguments for the tool.

        Returns:
            str: Whatever the tool replied.
        """
        with mock.patch.object(server, "_client", return_value=client), \
                mock.patch.object(server, "_agent_name", return_value=agent):
            return await tool(*args, **kwargs)

    def records(self):
        """The shared log parsed back into records.

        Returns:
            list[dict]: Every record currently on the bus.
        """
        return [json.loads(line) for line in self.log().splitlines()
                if line.strip()]

    def log(self):
        """The shared log exactly as it sits on disk.

        Returns:
            str: Every line of the bus file.
        """
        return pathlib.Path(self.requester.path).read_text(encoding="utf-8")

    def checks_for(self, client, agent):
        """The project checks waiting for one window, without consuming.

        Args:
            client (Bus): Connection to look with.
            agent (str): CLI name whose mail to look at.

        Returns:
            list[dict]: The waiting project_check records.
        """
        return [record for record in bus.peek(client, agent)
                if record["kind"] == "project_check"]

    async def delegate(self, text="Private task contents",
                       task_id="task_mcp_1"):
        """Delegate one task from the requester with a fixed id.

        Args:
            text (str): The task's withheld contents.
            task_id (str): Id to mint, so assertions can name it.

        Returns:
            tuple[str, str]: The task id and the tool's reply.
        """
        with mock.patch.object(bus, "new_task_id", return_value=task_id):
            response = await self.call(
                self.requester, "codex", server.delegate_task,
                "claude", text)
        return task_id, response

    async def accept_task(self, task_id):
        """Answer yes to the one check the assignee is holding.

        Args:
            task_id (str): The delegated task, for the assertion message.

        Returns:
            str: The confirm_project tool's reply.
        """
        checks = self.checks_for(self.assignee, "claude")
        self.assertEqual(len(checks), 1)
        check = checks[0]
        response = await self.call(
            self.assignee, "claude", server.confirm_project,
            check["confirmation_id"], True)
        self.assertIn("confirmed", response)
        response = await self.call(
            self.assignee, "claude", server.receive_messages)
        self.assertIn(task_id, response)
        self.assertEqual(
            bus.get_task(
                self.requester,
                task_id)["status"],
            "delivered")
        return check

    async def test_one_confirmation_covers_task_read_and_result(self):
        """One yes covers the task, its read and the result coming back."""
        task_text = "Private task: investigate the unique extraction defect."
        task_id, response = await self.delegate(task_text)
        task = bus.get_task(self.requester, task_id)
        self.assertEqual(task["status"], "awaiting_confirmation")
        self.assertEqual(task["sender_session"], self.requester.session)
        self.assertEqual(task["assignee_session"], self.assignee.session)
        self.assertEqual(task["assignee_agent"], "claude")
        self.assertIn("Awaiting project confirmation", response)
        self.assertNotIn(task_text, response)
        self.assertNotIn(task_text, self.log())
        self.assertEqual([record["kind"]
                         for record in self.records()], ["project_check"])
        self.assertIn(
            self.checks_for(
                self.assignee,
                "claude")[0]["confirmation_id"],
            response)

        await self.accept_task(task_id)
        self.assertIn(task_text, self.log())
        checks_before = sum(
            record["kind"] == "project_check" for record in self.records())
        result_text = "Private result: the defect has been repaired."
        response = await self.call(
            self.assignee, "claude", server.report_result,
            task_id, result_text)
        self.assertIn(
            "returned to " +
            bus.current_handle(
                self.requester,
                "codex"),
            response)
        self.assertEqual(
            bus.get_task(
                self.requester,
                task_id)["status"],
            "done")
        self.assertEqual(
            sum(record["kind"] == "project_check"
                for record in self.records()),
            checks_before)
        response = await self.call(
            self.requester, "codex", server.receive_messages)
        self.assertIn(result_text, response)

    async def test_changed_context_holds_result_until_requester_confirms(self):
        """A result is held again when the requester has moved on."""
        task_id, _ = await self.delegate()
        await self.accept_task(task_id)
        original_handle = bus.current_handle(self.requester, "codex")
        bus.set_name(self.requester, "codex-mcp-followup")
        replacement = self.window("replacement", "codex", original_handle)
        result_text = "Held result: details belong to the original requester."
        response = await self.call(
            self.assignee, "claude", server.report_result,
            task_id, result_text)
        self.assertIn("Awaiting project confirmation", response)
        self.assertNotIn(result_text, response)
        self.assertNotIn(result_text, self.log())
        task = bus.get_task(self.requester, task_id)
        self.assertEqual(task["status"], "delivered")
        self.assertEqual(
            task["result_confirmation_status"],
            "awaiting_confirmation")
        self.assertEqual(bus.peek(replacement, "codex"), [])
        checks = self.checks_for(self.requester, "codex")
        self.assertEqual(len(checks), 1)
        self.assertIn(checks[0]["confirmation_id"], response)
        self.assertEqual(checks[0]["to_session"], self.requester.session)

        response = await self.call(
            self.requester, "codex", server.confirm_project,
            checks[0]["confirmation_id"], True)
        self.assertIn("confirmed", response)
        self.assertEqual(
            bus.get_task(
                self.requester,
                task_id)["status"],
            "done")
        response = await self.call(
            self.requester, "codex", server.receive_messages)
        self.assertIn(result_text, response)
        self.assertEqual(bus.peek(replacement, "codex"), [])

    @mock.patch.object(routing, "find_message")
    async def test_task_ledger_routes_result_when_original_is_missing(
            self, find_message):
        """The ledger still routes a result after the log is compacted.

        Args:
            find_message (Mock): Stands in for the log lookup, hiding
                the one message the test compacts away.
        """
        find_message.side_effect = self.compacted_log
        task_id, _ = await self.delegate()
        await self.accept_task(task_id)
        task = bus.get_task(self.requester, task_id)
        original_handle = bus.current_handle(self.requester, "codex")
        bus.set_name(self.requester, "codex-after-compaction")
        replacement = self.window("replacement", "codex", original_handle)
        self.compacted_message_id = task["message_id"]

        response = await self.call(
            self.assignee, "claude", server.report_result,
            task_id, "Result after original message compaction")
        self.assertIn("Awaiting project confirmation", response)
        checks = self.checks_for(self.requester, "codex")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["to_session"], self.requester.session)
        self.assertEqual(bus.peek(replacement, "codex"), [])

    def send_then_accept_and_receive(self, *args, **kwargs):
        """Send, then confirm and drain it before the caller sees it.

        This compresses the whole handshake into the sender's own call,
        which is the race the test is checking: the task must still end
        up marked delivered rather than stuck at what it was when the
        send returned.

        Args:
            *args (object): Positional arguments for the real send.
            **kwargs (object): Keyword arguments for the real send.

        Returns:
            dict: The record the real send produced.
        """
        record = self.real_send(*args, **kwargs)
        project_confirmation.confirm_project(
            self.assignee, "claude", record["confirmation_id"], True)
        bus.receive_and_settle(self.assignee, "claude")
        return record

    @mock.patch.object(bus, "send")
    async def test_fast_confirmation_and_receive_do_not_lose_delivered_status(
            self, send):
        """A confirm and read racing the send still land as delivered.

        Args:
            send (Mock): Stands in for the send, running the whole
                confirm-and-read handshake before it returns.
        """
        send.side_effect = self.send_then_accept_and_receive

        task_id, _ = await self.delegate()

        self.assertEqual(
            bus.get_task(self.requester, task_id)["status"], "delivered")

    async def test_mixed_broadcast_reports_only_checks_still_waiting(self):
        """A broadcast names only the windows that have not yet agreed."""
        task_id, _ = await self.delegate()
        await self.accept_task(task_id)
        unconfirmed = self.window(
            "other",
            "claude",
            "claude-another-project",
            "other")
        payload = "Broadcast contents kept from the unconfirmed window."
        response = await self.call(
            self.requester, "codex", server.broadcast_message,
            payload, to="claude")
        check = self.checks_for(unconfirmed, "claude")[0]
        self.assertIn(check["confirmation_id"], response)
        self.assertNotIn("check None", response)
        self.assertNotIn(bus.current_handle(self.assignee, "claude"), response)
        self.assertNotIn(payload, response)
        self.assertNotIn(payload, [record["text"]
                         for record in bus.peek(unconfirmed, "claude")])
        self.assertIn(payload, [record["text"]
                      for record in bus.peek(self.assignee, "claude")])


if __name__ == "__main__":
    unittest.main()
