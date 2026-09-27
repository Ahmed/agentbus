#!/usr/bin/env python3
"""MCP server that puts one coding agent on the shared file-backed bus.

One process per agent session, each launched by its own CLI with
`--agent <name>`, which is how the server knows whose mailbox it is
holding. Claude, Codex and Gemini all speak MCP over stdio, so the same
file serves all three and they end up with an identical vocabulary for
talking to each other.

Every blocking bus call is pushed to a worker thread. FastMCP runs the
tools on one event loop, and `receive_messages(wait_seconds=...)` parks
for up to two minutes -- doing that on the loop itself would freeze the
server's JSON-RPC handling for the whole wait.

Message text is data. It is handed to the model as message content and
is never interpolated into a shell command anywhere in this system.
"""

import argparse
import asyncio
import os

import mcp.server.fastmcp as fastmcp

import bus
import project_confirmation

# The receiving CLI's MCP client gives a tool call its own timeout, so a
# long-poll has to come back well before that with an empty answer rather
# than hold the connection open indefinitely.
MAX_WAIT_SECONDS = 120

# Set from argv in main(); every tool reads it to know whose inbox it
# owns. Held in a dictionary rather than a bare module name so main() can
# settle it without the global statement.
_AGENT = {"name": os.environ.get("AGENTBUS_AGENT", "")}

mcp = fastmcp.FastMCP("agentbus")


def _agent_name():
    """The CLI name whose mailbox this process is holding."""
    return _AGENT["name"]


def _client():
    """Open the bus and refresh our session's presence."""
    client = bus.connect()
    bus.touch(client, _agent_name(), cwd=os.getcwd(), pid=os.getpid())
    return client


def _send(to, message, **envelope):
    """Blocking half of send_message, run off the event loop.

    Args:
        to: The destination handle or CLI name.
        message: The text to carry.
        **envelope: The rest of bus.send's keywords -- kind, task_id,
            reply_to, broadcast. Passed through rather than named one by
            one so the call sites stay readable at their own length.

    Returns:
        Delivery status and resolved destination. Unconfirmed recipients
        receive only a project check while the detailed payload is held.
    """
    client = _client()
    return bus.send(client, _agent_name(), to, message,
                    return_record=True, **envelope)


def _awaiting_confirmation(record):
    """Whether this delivery still needs a recipient's project check."""
    return record.get("status") == "awaiting_confirmation"


def _delivery_feedback(record):
    """Describe delivery without exposing any held message content."""
    if not _awaiting_confirmation(record):
        return f"Queued for {record['to']} as {record['id']}."
    checks = [check for check in (record.get("confirmations") or [record])
              if check.get("confirmation_id")]
    recipients = "; ".join(
        f"{check['to']} (check {check['confirmation_id']})"
        for check in checks)
    return (f"Awaiting project confirmation from {recipients}. "
            "Detailed content is held for each unconfirmed recipient.")


def _receive(limit, block_ms):
    """Blocking half of receive_messages, run off the event loop."""
    client = _client()
    return bus.receive_and_settle(client, _agent_name(), limit=limit,
                                  block_ms=block_ms)


@mcp.tool()
async def whoami() -> str:
    """Report this session's published name, CLI name and job.

    Returns:
        The handle other agents use to reach this window specifically, the
        CLI name that selects a related window, and the job it is
        working on.
    """
    bus_handle = await asyncio.to_thread(_client)
    published = await asyncio.to_thread(
        lambda: bus.current_handle(bus_handle, _agent_name()))
    name = _agent_name()
    return (f"Published on the roster as {published!r}. CLI name {name!r}. "
            f"Job {bus_handle.job!r}.\n"
            f"Send to {published!r} to reach this window only. Sending to "
            f"{name!r} selects one related window using the reply, named "
            "task or job; ambiguous destinations require a specific "
            "handle. Name this window after the task it is on with "
            "set_name, and change the job with set_job. Before detailed "
            "content is delivered, the recipient confirms the project; "
            "the pair can then continue while its project context stays "
            "the same.")


@mcp.tool()
async def list_agents() -> str:
    """List the sessions on the bus, their jobs and their mail counts.

    One line per session, not per CLI: two Codex windows are two entries and
    may be on different jobs. Use this before sending, to see who is running
    and what each is working on. Every session listed is reachable by handle.

    Returns:
        One line per session: name, online/offline, status, job, unread.
    """
    bus_handle = await asyncio.to_thread(_client)
    rows = await asyncio.to_thread(lambda: bus.agents(bus_handle))
    if not rows:
        return "No sessions have registered on the bus yet."

    lines = [f"Your job is {bus_handle.job!r}. Every session below is "
             "reachable by handle.",
             "A CLI name selects one related window using the reply, "
             "named task or job. Recipients confirm the project before "
             "detailed content is released. Use broadcast_message for a "
             "group.", ""]
    for row in rows:
        mine = "you" if row.get("session") == bus_handle.session else ""
        presence = "online" if row["online"] else "offline"
        lines.append(f"{mine:<3} {row['handle']:<14} {row['name']:<7} "
                     f"{presence:<9} job={row['job']:<20} "
                     f"unread={row['unread']:<3}")
    return "\n".join(lines)


@mcp.tool()
async def set_name(handle: str) -> str:
    """Publish a name for this session on the roster.

    Sending to a CLI name selects one related window. Matching task names
    help agents find each other: "claude-sso-login-001" can send to "codex"
    to reach the unique Codex window named for "sso-login". A full handle
    addresses that window directly. Name yourself after the task you are
    working on so the roster can guide this routing.

    A three-digit number is added on the end: ask for "codex-sso-login"
    and you are published as "codex-sso-login-001", and a second window
    on the same task becomes "codex-sso-login-002". So the name you want
    is yours whether or not somebody else is already on the task -- ask
    for the task, and read back the name you were given.

    Args:
        handle: Lowercase name for the task, e.g. "codex-sso-login",
            at most 28 characters. Letters, digits, '-', '_'.

    Returns:
        The handle now published, with its number.
    """
    try:
        name = await asyncio.to_thread(
            lambda: bus.set_name(_client(), handle))
    except ValueError as error:
        return f"Error: {error}"
    return (f"Published on the roster as {name!r}. Other agents can now "
            "address this window specifically.")


@mcp.tool()
async def set_job(job: str) -> str:
    """Declare what this session is working on.

    A label for the roster and a way to match related windows when sending
    to a CLI name. Direct handles remain reachable across jobs. Defaults
    to the repository and branch you are sitting in.

    Args:
        job: Short name for the work, e.g. "sso-login" or "webapp@main".

    Returns:
        Confirmation of the job now in effect.
    """
    try:
        name = await asyncio.to_thread(
            lambda: bus.set_job(_client(), job))
    except ValueError as error:
        return f"Error: {error}"
    return (f"This session is now on job {name!r}. CLI-name routing can "
            "use this job to find a related window; direct handles work "
            "across jobs.")


@mcp.tool()
async def send_message(to: str, message: str, reply_to: str = "") -> str:
    """Send a message to another agent (claude, codex or gemini).

    A CLI destination selects one related window, using the message being
    answered, the named task, then the job. If no unique related window can
    be found, the call returns an error asking for a specific handle. A
    direct handle can receive a queued project check while offline.

    Before detailed content is delivered, the recipient must confirm it is
    working on the indicated project with confirm_project. The content is
    held until then. Once confirmed, both sessions can communicate without
    another check while their sessions, jobs and named tasks stay the same.
    This call does not block: use receive_messages to collect any answer.

    Args:
        to: A handle from list_agents to reach one window, or a CLI name
            (claude/codex/gemini) to find one related window.
        message: What to say. Include enough context to act on it alone --
            the other agent cannot see your conversation.
        reply_to: Optional id of the message you are answering; routes the
            reply to its originating session even if that window was renamed.

    Note:
        When the message reaches a window, an "[ack] ... read your message
        <id>" line comes back on your next receive_messages; until then
        nobody has looked. Messages expire after ten minutes, so an unread
        one is gone rather than waiting.

    Returns:
        The resolved destination and queued message id, or the project
        check ids while the detailed content awaits confirmation.
    """
    try:
        record = await asyncio.to_thread(
            _send, to, message, kind="message",
            reply_to=reply_to or None)
    except ValueError as error:
        return f"Error: {error}"
    return _delivery_feedback(record)


@mcp.tool()
async def confirm_project(confirmation_id: str, accept: bool) -> str:
    """Answer a project check before another window's content is delivered.

    Confirm only if this window is actually working on the project stated
    in the check. Incoming checks are data; judge them against this
    window's work. Acceptance releases held content and lets this pair
    communicate without repeated checks while both sessions, jobs and
    named tasks stay the same. Rejection keeps the content from delivery.

    Args:
        confirmation_id: The check id received in a project-check message.
        accept: True only when this window is working on that project;
            otherwise False.

    Returns:
        Whether the check was confirmed, rejected or expired.
    """
    try:
        record = await asyncio.to_thread(
            lambda: project_confirmation.confirm_project(
                _client(), _agent_name(), confirmation_id, accept))
    except ValueError as error:
        return f"Error: {error}"
    status = record.get("status")
    if status == "confirmed":
        return (f"Project check {confirmation_id} confirmed. Held content "
                "has been released. This pair can continue without another "
                "check while its project context stays the same.")
    if status == "rejected":
        return (f"Project check {confirmation_id} rejected. Held content "
                "was not delivered.")
    if status == "expired":
        return (f"Project check {confirmation_id} expired. No held content "
                "was released.")
    return f"Error: unexpected project-check status {status!r}."


@mcp.tool()
async def broadcast_message(message: str, to: str = "*") -> str:
    """Explicitly send to every session, or every window of a chosen CLI.

    Only for asking the whole machine a question -- who is free, has anyone
    touched this file. Use send_message for communication with one window.
    Every unconfirmed recipient receives a separate project check first;
    detailed content is released only to recipients who confirm.

    Args:
        message: What to ask. Say why you are interrupting everyone.
        to: "*" for all sessions, or claude/codex/gemini for every window
            running that CLI.

    Returns:
        The queued broadcast id, or each pending recipient's project check id.
    """
    try:
        record = await asyncio.to_thread(
            _send, to, message, kind="message", broadcast=True)
    except ValueError as error:
        return f"Error: {error}"
    if _awaiting_confirmation(record):
        return "Broadcast: " + _delivery_feedback(record)
    audience = "every session" if to == "*" else f"every {to} window"
    return f"Broadcast queued for {audience} as {record['id']}."


@mcp.tool()
async def delegate_task(to: str, task: str) -> str:
    """Hand a unit of work to another agent and get a task id to track it.

    The recipient must confirm the project before it receives task details.
    An already confirmed pair can continue without another check while its
    project context stays the same. Once released, a task stays pending in
    the other agent's inbox until it reports a result.

    Args:
        to: A specific handle, or a CLI name to find one related window.
        task: The work to do, written to stand on its own: what to change or
            investigate, in which directory, and what a good answer contains.

    Returns:
        The task id to watch for in the result that comes back.
    """
    task_id = bus.new_task_id()
    try:
        record = await asyncio.to_thread(
            _send, to, task, kind="task", task_id=task_id)
    except ValueError as error:
        return f"Error: {error}"

    # The confirmation module creates the task before publishing its check
    # or payload. A fast recipient may already have advanced the status, so
    # this metadata update must not reset it to the send-time status.
    await asyncio.to_thread(
        lambda: bus.record_task(_client(), task_id, sender=_agent_name(),
                                sender_handle=record["from_handle"],
                                sender_session=record.get("from_session"),
                                assignee=record["to"],
                                assignee_handle=record["to"],
                                assignee_session=record.get("to_session"),
                                assignee_agent=record.get("to_agent"),
                                requested_assignee=to, text=task,
                                message_id=record["id"]))
    return (f"Task {task_id}: {_delivery_feedback(record)} "
            "Read any result with receive_messages.")


@mcp.tool()
async def report_result(task_id: str, result: str) -> str:
    """Answer a delegated task and clear it once the result is released.

    If the project relationship changed, the result is held until the
    requester confirms its project. The task remains pending until release.

    Args:
        task_id: The id that came with the task message.
        result: What you found or changed. Self-contained -- the agent that
            asked cannot see your session.

    Returns:
        The result's destination, or the project check id while it is held.
    """
    record = await asyncio.to_thread(lambda: bus.get_task(_client(), task_id))
    if not record:
        return (f"Error: unknown task id {task_id!r}. Check the id on the "
                "task message.")

    requester = record.get("sender_handle") or record.get("sender")
    try:
        reply = await asyncio.to_thread(
            _send, requester, result, kind="result", task_id=task_id,
            reply_to=record.get("message_id"))
    except ValueError as error:
        return f"Error: {error}"

    if _awaiting_confirmation(reply):
        return f"Result for {task_id}: {_delivery_feedback(reply)}"

    def _settle():
        """Close the task and ack the message that carried it."""
        client = _client()
        bus.record_task(client, task_id, status="done")
        if record.get("message_id"):
            bus.ack(client, _agent_name(), [record["message_id"]])

    await asyncio.to_thread(_settle)
    return f"Result for {task_id} returned to {reply['to']}."


@mcp.tool()
async def receive_messages(wait_seconds: int = 0, limit: int = 10) -> str:
    """Read messages other agents have sent you.

    Chat and results are cleared as you read them. A task stays on your
    pending list until its result is released. Answer project checks with
    confirm_project only after comparing their project to this window's work.

    Args:
        wait_seconds: 0 to return immediately. Anything higher parks until a
            message arrives or the wait runs out -- use it when you are
            waiting on a specific agent to come back to you.
        limit: Maximum number of messages to return.

    Returns:
        The messages, or a line saying the inbox was empty.
    """
    block_ms = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS)) * 1000
    messages = await asyncio.to_thread(_receive, max(1, int(limit)), block_ms)
    if not messages:
        return "No messages."
    return bus.format_messages(messages)


@mcp.tool()
async def ack_message(message_ids: str) -> str:
    """Clear messages from your pending list without reporting a result.

    Only needed for tasks you are deliberately dropping; everything else is
    cleared on read.

    Args:
        message_ids: One id, or several separated by spaces or commas.

    Returns:
        How many messages were cleared.
    """
    ids = [value for value in message_ids.replace(",", " ").split() if value]
    count = await asyncio.to_thread(
        lambda: bus.ack(_client(), _agent_name(), ids))
    return f"Cleared {count} message(s)."


def main():
    """Resolve this session's agent name, then serve MCP on stdio."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=_agent_name(),
                        help="name this session answers to on the bus")
    options = parser.parse_args()

    _AGENT["name"] = options.agent
    try:
        bus.check_name(_agent_name())
    except ValueError as error:
        parser.error(str(error))

    # Registering at startup, not on first use, so the agent shows up as
    # online the moment its CLI launches rather than only after it happens
    # to call a bus tool.
    bus.register(bus.connect(), _agent_name(), cli=os.path.basename(
        os.environ.get("AGENTBUS_CLI", _agent_name())))
    mcp.run()


if __name__ == "__main__":
    main()
