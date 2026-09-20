#!/usr/bin/env python3
"""MCP server that puts one coding agent on the shared Redis bus.

One process per agent session, each launched by its own CLI with
`--agent <name>`, which is how the server knows whose mailbox it is
holding. Claude, Codex and Gemini all speak MCP over stdio, so the same
file serves all three and they end up with an identical vocabulary for
talking to each other.

Every blocking Redis call is pushed to a worker thread. FastMCP runs the
tools on one event loop, and `receive_messages(wait_seconds=...)` parks
for up to two minutes -- doing that on the loop itself would freeze the
server's JSON-RPC handling for the whole wait.

Message text is data. It is handed to the model as message content and
is never interpolated into a shell command anywhere in this system.
"""

import argparse
import asyncio
import os
import sys

import mcp.server.fastmcp as fastmcp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bus


# The receiving CLI's MCP client gives a tool call its own timeout, so a
# long-poll has to come back well before that with an empty answer rather
# than hold the connection open indefinitely.
MAX_WAIT_SECONDS = 120

# Set from argv in main(); every tool reads it to know whose inbox it owns.
AGENT_NAME = os.environ.get("AGENTBUS_AGENT", "")

mcp = fastmcp.FastMCP("agentbus")


def _client():
    """Open a short-lived Redis client and refresh our presence with it."""
    client = bus.connect()
    bus.touch(client, AGENT_NAME, cwd=os.getcwd(), pid=os.getpid())
    return client


def _send(to, message, kind, task_id, reply_to):
    """Blocking half of send_message, run off the event loop."""
    client = _client()
    return bus.send(client, AGENT_NAME, to, message, kind=kind,
                    task_id=task_id, reply_to=reply_to)


def _receive(limit, block_ms):
    """Blocking half of receive_messages, run off the event loop."""
    client = _client()
    return bus.receive_and_settle(client, AGENT_NAME, limit=limit,
                                  block_ms=block_ms)


@mcp.tool()
async def whoami() -> str:
    """
    Report this session's published name, CLI name and job.

    Returns:
        The handle other agents use to reach this window specifically, the
        CLI name that reaches every window running it, and the job it is
        working on.
    """
    bus_handle = await asyncio.to_thread(_client)
    published = await asyncio.to_thread(
        lambda: bus.current_handle(bus_handle, AGENT_NAME))
    return ("Published on the roster as %r. CLI name %r. Job %r.\n"
            "Send to %r to reach this window only, or to %r to reach every "
            "window running that CLI. Every session on this machine can "
            "reach every other; the job is a label saying what each one is "
            "busy with. Name this window after the task it is on with "
            "set_name, and change the job with set_job."
            % (published, AGENT_NAME, bus_handle.job, published, AGENT_NAME))


@mcp.tool()
async def list_agents() -> str:
    """
    List the sessions on the bus, what job each is on, and their mail counts.

    One line per session, not per CLI: two Codex windows are two entries and
    may be on different jobs. Use this before sending, to see who is running
    and what each is working on. Every session listed is reachable.

    Returns:
        One line per session: name, online/offline, status, job, unread.
    """
    bus_handle = await asyncio.to_thread(_client)
    rows = await asyncio.to_thread(lambda: bus.agents(bus_handle))
    if not rows:
        return "No sessions have registered on the bus yet."

    lines = ["Your job is %r. Every session below is reachable; the job "
             "says what each one is busy with." % bus_handle.job,
             "Send to a handle to reach one window, or to a CLI name to "
             "reach every window running it.", ""]
    for row in rows:
        mine = row.get("session") == bus_handle.session
        lines.append(
            "%-3s %-14s %-7s %-9s job=%-20s unread=%-3s"
            % ("you" if mine else "",
               row["handle"], row["name"],
               "online" if row["online"] else "offline",
               row["job"], row["unread"]))
    return "\n".join(lines)


@mcp.tool()
async def set_name(handle: str) -> str:
    """
    Publish a name for this session on the roster.

    Sending to a CLI name reaches every window running it, which is right
    for "any codex will do" and wrong for "the codex already looking at
    this file". A handle makes the second possible. Defaults to something
    like "codex-7145"; name yourself after the task you are working on
    instead, so the roster says who is doing what and another agent can
    reach the right window.

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
        return "Error: %s" % error
    return ("Published on the roster as %r. Other agents can now address "
            "this window specifically." % name)


@mcp.tool()
async def set_job(job: str) -> str:
    """
    Declare what this session is working on.

    A label for the roster, so another window can see what this one is busy
    with before interrupting it. It does not gate delivery -- every session
    on this machine is reachable from every other. Defaults to the
    repository and branch you are sitting in.

    Args:
        job: Short name for the work, e.g. "sso-login" or "webapp@main".

    Returns:
        Confirmation of the job now in effect.
    """
    try:
        name = await asyncio.to_thread(
            lambda: bus.set_job(_client(), job))
    except ValueError as error:
        return "Error: %s" % error
    return ("This session is now on job %r. Only sessions on that job can "
            "reach it." % name)


@mcp.tool()
async def send_message(to: str, message: str, reply_to: str = "") -> str:
    """
    Send a message to another agent (claude, codex or gemini).

    The message waits in the target's inbox if it is offline, so this never
    fails just because the other agent is not running. It does not block:
    use receive_messages to collect any answer.

    Args:
        to: A handle from list_agents to reach one window, or a CLI name
            (claude/codex/gemini) to reach every window running it.
        message: What to say. Include enough context to act on it alone --
            the other agent cannot see your conversation.
        reply_to: Optional id of the message you are answering.

    Note:
        When the message reaches a window, an "[ack] ... read your message
        <id>" line comes back on your next receive_messages; until then
        nobody has looked. Messages expire after ten minutes, so an unread
        one is gone rather than waiting.

    Returns:
        The id of the delivered message.
    """
    try:
        message_id = await asyncio.to_thread(
            _send, to, message, "message", None, reply_to or None)
    except ValueError as error:
        return "Error: %s" % error
    return "Delivered to %s as %s." % (to, message_id)


@mcp.tool()
async def broadcast_message(message: str) -> str:
    """
    Send to EVERY session on the bus at once.

    Only for asking the whole machine a question -- who is free, has anyone
    touched this file. An ordinary message belongs in send_message, addressed
    to the one window or CLI that should answer it.

    Args:
        message: What to ask. Say why you are interrupting everyone.

    Returns:
        The id of the delivered message.
    """
    try:
        message_id = await asyncio.to_thread(
            lambda: bus.send(_client(), AGENT_NAME, AGENT_NAME, message,
                             job=bus.BROADCAST_JOB))
    except ValueError as error:
        return "Error: %s" % error
    return "Broadcast to every session as %s." % message_id


@mcp.tool()
async def delegate_task(to: str, task: str) -> str:
    """
    Hand a unit of work to another agent and get a task id to track it.

    Unlike send_message, a task stays pending in the other agent's inbox
    until it reports a result, so it survives that session being killed.

    Args:
        to: Target agent name, as shown by list_agents.
        task: The work to do, written to stand on its own: what to change or
            investigate, in which directory, and what a good answer contains.

    Returns:
        The task id to watch for in the result that comes back.
    """
    task_id = bus.new_task_id()
    try:
        message_id = await asyncio.to_thread(
            _send, to, task, "task", task_id, None)
    except ValueError as error:
        return "Error: %s" % error

    await asyncio.to_thread(
        lambda: bus.record_task(_client(), task_id, sender=AGENT_NAME,
                                assignee=to, text=task, status="sent",
                                message_id=message_id))
    return ("Task %s sent to %s. The result arrives in your inbox; read it "
            "with receive_messages." % (task_id, to))


@mcp.tool()
async def report_result(task_id: str, result: str) -> str:
    """
    Answer a task another agent delegated to you, and clear it from your inbox.

    Args:
        task_id: The id that came with the task message.
        result: What you found or changed. Self-contained -- the agent that
            asked cannot see your session.

    Returns:
        Confirmation naming the agent the result went back to.
    """
    record = await asyncio.to_thread(lambda: bus.get_task(_client(), task_id))
    if not record:
        return ("Error: unknown task id %r. Check the id on the task message."
                % task_id)

    requester = record.get("sender")
    try:
        await asyncio.to_thread(
            _send, requester, result, "result", task_id, None)
    except ValueError as error:
        return "Error: %s" % error

    def _settle():
        """Close the task and ack the message that carried it."""
        client = _client()
        bus.record_task(client, task_id, status="done")
        if record.get("message_id"):
            bus.ack(client, AGENT_NAME, [record["message_id"]])

    await asyncio.to_thread(_settle)
    return "Result for %s returned to %s." % (task_id, requester)


@mcp.tool()
async def receive_messages(wait_seconds: int = 0, limit: int = 10) -> str:
    """
    Read messages other agents have sent you.

    Chat and results are cleared as you read them. A task stays on your
    pending list until you call report_result, so you cannot lose one.

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
    """
    Clear messages from your pending list without reporting a result.

    Only needed for tasks you are deliberately dropping; everything else is
    cleared on read.

    Args:
        message_ids: One id, or several separated by spaces or commas.

    Returns:
        How many messages were cleared.
    """
    ids = [value for value in message_ids.replace(",", " ").split() if value]
    count = await asyncio.to_thread(
        lambda: bus.ack(_client(), AGENT_NAME, ids))
    return "Cleared %d message(s)." % count


def main():
    """Resolve this session's agent name, then serve MCP on stdio."""
    global AGENT_NAME

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=AGENT_NAME,
                        help="name this session answers to on the bus")
    options = parser.parse_args()

    AGENT_NAME = options.agent
    try:
        bus.check_name(AGENT_NAME)
    except ValueError as error:
        parser.error(str(error))

    # Registering at startup, not on first use, so the agent shows up as
    # online the moment its CLI launches rather than only after it happens
    # to call a bus tool.
    bus.register(bus.connect(), AGENT_NAME, cli=os.path.basename(
        os.environ.get("AGENTBUS_CLI", AGENT_NAME)))
    mcp.run()


if __name__ == "__main__":
    main()
