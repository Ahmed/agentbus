#!/usr/bin/env python3
"""Expose the shared file bus to shell commands."""

import json
import os
import pathlib
import sys
import time

import agentbus_constants as constants
import agentbus_context as context
import agentbus_delivery as delivery
import agentbus_events as events
import agentbus_identity as identity
import agentbus_messages as messages
import agentbus_notify as notify
import agentbus_presence as presence
import agentbus_routing as routing
import agentbus_state as state
import agentbus_storage as storage
import project_confirmation

_CONTEXT_LOCKS = context.CONTEXT_LOCKS
_context_locked = context.locked

AGENT_NAMES = constants.AGENT_NAMES
BROADCAST_JOB = constants.BROADCAST_JOB
BUS_DIR = constants.BUS_DIR
BUS_FILE = constants.BUS_FILE
CLI_NAMES = constants.CLI_NAMES
COMPACT_BYTES = constants.COMPACT_BYTES
MESSAGE_KINDS = constants.MESSAGE_KINDS
MESSAGE_TTL_SECONDS = constants.MESSAGE_TTL_SECONDS
NAME_NUMBER = constants.NAME_NUMBER
NAME_PATTERN = constants.NAME_PATTERN
PRESENCE_REAP_SECONDS = constants.PRESENCE_REAP_SECONDS
PRESENCE_TTL_SECONDS = constants.PRESENCE_TTL_SECONDS
STATE_DIR = constants.STATE_DIR
TASK_FILE = constants.TASK_FILE

default_handle = state.default_handle
current_handle = state.current_handle
current_task = state.current_task
check_name = state.check_name
default_job = state.default_job
session_dead = identity.session_dead
session_pid = identity.session_pid
bind_session = identity.bind_session
consumed_ids = storage.consumed_ids
record_task = storage.record_task
get_task = storage.get_task
new_task_id = storage.new_task_id
touch = presence.touch
register = presence.register
ensure_handle = presence.ensure_handle
names_a_chore = presence.names_a_chore
resolve_recipient = routing.resolve_recipient
send = messages.send
_send_direct = messages.send_direct
format_messages = messages.format_messages
watch = messages.watch
receive = delivery.receive
receive_and_settle = delivery.receive_and_settle
ack = delivery.ack
post_receipts = delivery.post_receipts
peek = delivery.peek
unread_count = delivery.unread_count


class Bus:
    """Paths and identity for one process talking to the bus.

    Exists so callers keep the shape they had when this was a Redis
    client: every function takes it as its first argument.
    """

    def __init__(self, directory=None, session=None, cwd=None):
        """Create the bus directory if this is the first process to arrive.

        Args:
            directory (str or None): Bus directory; defaults to /tmp/agentbus.
            session (str or None): Explicit session id. A hook is handed one
                by its CLI
                and should pass it, because guessing from the process tree
                is wrong under Codex -- see _session_key.
            cwd (str or None): The session's working directory. A hook is
                handed this
                too, and must pass it: the hook process runs wherever the
                CLI happened to spawn it, which is not necessarily where
                the session is working, and the job is derived from it.
        """
        self.directory = directory or BUS_DIR
        self.path = os.path.join(self.directory, "bus.jsonl")
        self.state = os.path.join(self.directory, "state")
        os.makedirs(self.state, exist_ok=True)
        if not os.path.exists(self.path):
            pathlib.Path(self.path).touch()
        self.session = session or identity.session_key(self.state)
        # An explicit job set by this session wins over the guess from cwd,
        # so two windows in one repo can split into separate conversations.
        self.cwd = os.path.abspath(cwd or os.getcwd())
        declared = (os.environ.get("AGENTBUS_JOB")
                    or state.read_job(self.state, self.session, self.cwd))
        # Guessed from the directory when the session has not said. It is
        # a label on the roster either way, so a wrong guess costs nothing
        # more than a misleading line.
        self.job = declared or state.default_job(self.cwd)
        # Every session publishes a handle of its own on the roster.
        # "codex" is not an address when three windows answer to it, so a
        # sender that means one particular window has a name to use.
        self.handle = None
        self.parent_session = None

    def current_handle(self, agent):
        """Expose the published name to clients sharing this connection.

        Args:
            agent (str): CLI name used for an unnamed window.

        Returns:
            str: Published or generated window handle.
        """
        return state.current_handle(self, agent)

    def for_session(self, session):
        """Inspect another inbox without changing this connection's identity.

        Args:
            session (str): Conversation whose inbox should be opened.

        Returns:
            Bus: Connection to the same directory for that conversation.
        """
        return type(self)(self.directory, session=session, cwd=self.cwd)


def connect(directory=None, session=None, cwd=None):
    """Open the bus for this process.

    Args:
        directory (str or None): Bus directory, or the configured default.
        session (str or None): Conversation identity; defaults to this window.
        cwd (str or None): Working directory used to infer the job.

    Returns:
        Bus: Connection for this directory and conversation.
    """
    return Bus(directory, session, cwd)


def agents(bus):
    """Include unread counts without letting roster inspection consume mail.

    Args:
        bus (Bus): Connection whose shared roster is inspected.

    Returns:
        list[dict]: Window presence and unread counts, sorted by name.
    """
    rows = presence.roster(bus)
    for row in rows:
        row["unread"] = delivery.unread_count(
            bus, row["name"], row.get("session"))
    return rows


def _as_agent(bus, name):
    """Map a name typed as this session's own identity to its agent name.

    The roster prints the handle first and the CLI name second, so a
    window reading its own row types the handle where the agent name
    belongs. Left alone that mints a second identity with its own cursor
    and an empty inbox, which is indistinguishable from having no mail --
    and sends the window off to grep the log by hand.

    Only this session's own handles are resolved. Another window's handle
    is left as typed, because sending to a handle is how a message
    reaches one window rather than every window running a CLI.

    Args:
        bus (Bus): The bus, for the session id the handles are looked up under.
        name (str): The name supplied as the caller's own identity.

    Returns:
        The agent name to act as, unchanged unless name is a handle this
        session publishes.
    """
    check_name(name)
    prefix = "presence."
    suffix = "." + bus.session
    known = {}
    for entry in os.listdir(bus.state):
        if not entry.startswith(prefix):
            continue
        if not entry.endswith(suffix):
            continue
        agent = entry[len(prefix):-len(suffix)]
        try:
            file_path = os.path.join(bus.state, entry)
            with open(file_path, encoding="utf-8") as opened:
                record = json.load(opened)
        except (IOError, OSError, ValueError):
            record = {}
        published = record.get("handle") or default_handle(agent, bus.session)
        known[published] = agent
    if name in known.values():
        return name
    if name in known:
        return known[name]
    # A window that was told to call itself claude-<task> types that
    # where its agent name belongs. Taken literally it registers a second
    # identity for the same window, which shows up on the roster as a
    # duplicate and reads mail from an inbox nobody writes to.
    for agent in set(known.values()) or constants.AGENT_NAMES:
        if name.startswith(agent + "-"):
            return agent
    return name


def _print_message(message):
    """Show live traffic without consuming any recipient's copy.

    Args:
        message (dict): Envelope observed at the end of the log.
    """
    timestamp = time.strftime("%H:%M:%S")
    sender = message.get("from", "?")
    target = message.get("to", "?")
    kind = message.get("kind", "message")
    body = message.get("text", "").replace("\n", " ")[:160]
    print(f"{timestamp}  {sender} -> {target}  [{kind}]  {body}", flush=True)


def _send_command(client, argv):
    """Report routing errors as shell status codes instead of tracebacks.

    Args:
        client (Bus): Connection for the calling window.
        argv (list[str]): Send or broadcast command and its arguments.

    Returns:
        int: Shell exit status.
    """
    command = argv[0]
    if len(argv) < 4:
        print(f"usage: bus.py {command} <from> <to> <text>", file=sys.stderr)
        return 2
    try:
        record = send(client, _as_agent(client, argv[1]), argv[2],
                      " ".join(argv[3:]), broadcast=command == "broadcast",
                      return_record=True)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(record["id"])
    for check in record.get("confirmations", []):
        if check.get("confirmation_id"):
            print((
                f'Waiting for {check['to']} to confirm the project: '
                f'{check['confirmation_id']}'
            ), file=sys.stderr)
    print((
        f'{record['from_handle']} -> {record['to']} ('
        f'{record.get('status', record['routing'])})'
    ),
        file=sys.stderr)
    _report_outcome(client, record, SEND_WAIT_SECONDS)
    return 0


def _outcome_line(client, check):
    """One line saying what became of a held message.

    Args:
        client (Bus): Connection for the calling window.
        check (dict): A confirmation entry from the send record.

    Returns:
        str or None: The line to print, or None while it is undecided.
    """
    record = project_confirmation.pending_state(
        client, check["confirmation_id"])
    if record is None:
        return f'{check['to']}: no record of that check any more'

    status = record.get("status")
    if status == "confirmed":
        return f'{check['to']}: confirmed, message delivered'
    if status == "rejected":
        return f'{check['to']}: declined the project, nothing was shared'
    if status == "expired":
        reason = record.get("reason", "expired")
        return f'{check['to']}: {reason}, nothing was shared'
    return None


def _report_outcome(client, record, seconds):
    """Wait a little and say whether the message actually landed.

    A send returns the moment the question is asked, which tells the
    sender nothing about whether anyone answered it. For an agent that
    matters more than it would for a person: it has no terminal to watch
    and no reason to look again, so silence reads as success and a
    message that was never delivered looks exactly like one that was.

    Bounded on purpose. This is a courtesy at the end of a send, not a
    reason for the shell to hang: what is not decided by the deadline is
    reported as undecided rather than waited out.

    Args:
        client (Bus): Connection for the calling window.
        record (dict): The queued send record.
        seconds (float): Longest time to wait for an answer.
    """
    checks = [item for item in record.get("confirmations", [])
              if item.get("confirmation_id")]
    if not checks or seconds <= 0:
        return

    deadline = time.time() + seconds
    pending = list(checks)
    while pending:
        for check in list(pending):
            line = _outcome_line(client, check)
            if line is not None:
                print(line, file=sys.stderr)
                pending.remove(check)
        if not pending:
            return

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        # The status is written by the other window, and every write to
        # the bus rings, so this returns the moment there is something
        # to look at rather than on a timer.
        notify.wait_or_sleep(_as_agent(client, record["from"]),
                             client.session, remaining,
                             min(max(remaining, 0), 0.25))

    for check in pending:
        print(f'{check['to']}: no answer yet, nothing shared so far',
              file=sys.stderr)


def _name_command(client, argv):
    """Print this window's name and task, setting the task when given.

    The name is the bus's to give and is never changed here; what the
    window types is its task. A refused task is an ordinary, recoverable
    outcome rather than a traceback.

    Args:
        client (Bus): Connection whose name and task are shown.
        argv (list[str]): Name command and optional task words.

    Returns:
        int: Shell exit status.
    """
    rows = [row for row in presence.routing_rows(client)
            if row["session"] == client.session]
    if rows:
        agent = rows[0]["agent"]
    else:
        agent = next((name for name in constants.AGENT_NAMES
                      if len(argv) > 1 and argv[1].startswith(name + "-")),
                     "claude")
    handle = ensure_handle(client, agent)
    if len(argv) > 1:
        try:
            set_task(client, " ".join(argv[1:]).replace(" ", "-"))
        except ValueError as error:
            print(f"cannot set that task: {error}", file=sys.stderr)
            return 1
    task = state.current_task(client, agent)
    print(f"{handle} task={task or '(none yet)'}")
    return 0


def _confirm_command(client, argv):
    """Release held contents only after an explicit yes from the recipient.

    Args:
        client (Bus): Connection for the confirming window.
        argv (list[str]): Confirm command, recipient, request id, and decision.

    Returns:
        int: Shell exit status.
    """
    if len(argv) != 4 or argv[3] not in ("yes", "no"):
        print("usage: bus.py confirm <agent> <confirmation-id> yes|no",
              file=sys.stderr)
        return 2
    try:
        result = project_confirmation.confirm_project(
            client, _as_agent(client, argv[1]), argv[2], argv[3] == "yes")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Project {result['status']}: {result.get('id', argv[2])}")
    return 0


# How long a send waits to find out whether its message landed, before
# saying so and returning. Short: the sender is usually an agent part
# way through a turn, and the answer is worth a moment but not a stall.
SEND_WAIT_SECONDS = float(os.environ.get("AGENTBUS_SEND_WAIT", "6"))


def _wait_command(client, argv):
    """Keep explicit shell waits silent until actual mail arrives.

    Args:
        client (Bus): Window whose inbox is observed without consuming it.
        argv (list[str]): Command, agent, and optional finite timeout.

    Returns:
        int: Zero for mail or owner exit, one for an explicitly requested
        timeout, and two for invalid arguments. The default has no timeout.
    """
    if len(argv) < 2:
        print("usage: bus.py wait <agent> [seconds]")
        return 2
    agent = _as_agent(client, argv[1])
    deadline = None
    try:
        if len(argv) > 2:
            deadline = time.monotonic() + float(argv[2])
    except ValueError:
        print("usage: bus.py wait <agent> [seconds]")
        return 2
    with events.Listener(client, agent, pid=session_pid() or 0) as listener:
        while True:
            waiting = peek(client, agent, client.session)
            actionable = [item for item in waiting
                          if item.get("kind") not in ("ack", "project_status")]
            if actionable:
                print(f"{len(actionable)} waiting for {agent}. "
                      f"Read with: bus.py read {agent}")
                return 0
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 1
            if not listener.wait(remaining):
                return 0


def _utility_command(client, argv):
    """Handle administrative commands separately from message delivery.

    Args:
        client (Bus): Connection for the calling window.
        argv (list[str]): Administrative command and arguments.

    Returns:
        int: Shell exit status, including two for an unknown command.
    """
    command = argv[0]
    if command == "watch":
        print(f"watching {client.path} (ctrl-c to stop)", flush=True)
        watch(client, _print_message)
    elif command == "register":
        print(register(client, argv[1]))
    elif command == "job":
        print(set_job(client, " ".join(argv[1:])) if len(argv) > 1
              else client.job)
    elif command == "path":
        print(client.path)
    else:
        print("usage: bus.py agents|watch|path|job [name]|name [task]|"
              "send <from> <to> <text>|broadcast <from> <to> <text>|"
              "read <agent>|register <agent>|confirm <agent> <id> yes|no|"
              "wait <agent> [seconds]")
        return 2
    return 0


def _agents_command(client):
    """Print the roster, one window per line.

    Args:
        client (Bus): Connection for the calling window.

    Returns:
        int: Shell exit status.
    """
    for row in agents(client):
        status = "online" if row["online"] else "offline"
        handle = row.get("display_handle") or row["handle"]
        print((
            f'{handle:<15} {row['name']:<7} {status:<8} task='
            f'{row.get('task') or '-':<16} job={row['job']:<22} '
            f'unread={row['unread']!s:<3}'
        ))
    return 0


def _read_command(client, argv):
    """Consume and print this window's waiting mail.

    Args:
        client (Bus): Connection for the calling window.
        argv (list[str]): Read command and the agent name.

    Returns:
        int: Shell exit status.
    """
    print(format_messages(receive_and_settle(
        client, _as_agent(client, argv[1]), limit=50)) or "(no messages)")
    return 0


def _main(argv):
    """Expose the same bus operations to integrations without MCP.

    Args:
        argv (list[str]): Command arguments excluding the executable name.

    Returns:
        int: Shell exit status.
    """
    client = connect()
    command = argv[0] if argv else "agents"
    handlers = {"send": _send_command, "broadcast": _send_command,
                "name": _name_command, "task": _name_command,
                "confirm": _confirm_command,
                "wait": _wait_command}
    if command == "agents":
        return _agents_command(client)
    if command in ("read", "drain"):
        return _read_command(client, argv)
    if command in handlers:
        return handlers[command](client, argv)
    return _utility_command(client, argv)


@context.locked
def set_task(client, task):
    """Publish what this window is on, and forget approvals for the last.

    Args:
        client (Bus): Connection whose declared task changes.
        task (str): The work, e.g. "sso-login".

    Returns:
        str: The task now published.
    """
    handle = state.read_handle(client.state, client.session) or ""
    previous = state.task_of(client.state, client.session,
                             handle.split("-", 1)[0], handle)
    published = presence.set_task(client, task)
    links = os.path.join(client.state, "project_links.json")
    if previous != published and os.path.exists(links):
        project_confirmation.invalidate_relationships(client)
    return published


# The command and MCP tool that set the task are still called "name":
# every brief and every window's habits say `name`, and the name is what
# the operator reads the task beside.
set_name = set_task


@context.locked
def set_job(client, job):
    """Require renewed confirmation when this window changes its project.

    Args:
        client (Bus): Connection whose declared work changes.
        job (str): New project label.

    Returns:
        str: Effective project label.
    """
    previous = client.job
    published = state.set_job(client, job)
    os.path.join(client.state, "project_links.json")
    if (previous != published and os.path.exists(
            os.path.join(client.state, "project_links.json"))):
        project_confirmation.invalidate_relationships(client)
    return published


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
