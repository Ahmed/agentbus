#!/usr/bin/env python3
"""A single append-only file that the coding agents on this machine talk through.

Claude, Codex and Gemini each run as an ordinary terminal session with no
shared memory, so they need something outside themselves to meet in. That
something is one file: `/tmp/agentbus/bus.jsonl`, one JSON message per line.

A log, not a queue, and that distinction is the whole point. The previous
version used Redis streams with a consumer group, which hands each message
to exactly one reader -- so with two Codex windows open, whichever looked
first swallowed the message and the other saw an empty inbox. A log has no
such behaviour: every reader keeps its own position in it and they all see
everything.

Each reader's position lives in `state/cursor.<agent>.<session>`, where the
session is the controlling CLI process. That keeps two windows of the same
CLI independent while the MCP server and the session hooks inside one
window share a position, so a message is not delivered twice.

Nothing here needs a server, a daemon or a database. Appends are made under
an exclusive lock and are ordinary line writes.
"""

import errno
import fcntl
import json
import os
import re
import socket
import sys
import time
import uuid


# One file under /tmp, by the owner's decision. /tmp is cleared on reboot,
# which suits a channel whose contents are worthless by the next morning.
BUS_DIR = os.environ.get("AGENTBUS_DIR", "/tmp/agentbus")
BUS_FILE = os.path.join(BUS_DIR, "bus.jsonl")
STATE_DIR = os.path.join(BUS_DIR, "state")
TASK_FILE = os.path.join(STATE_DIR, "tasks.json")

# Ten minutes. This is the backstop, not the usual way a message leaves
# the bus: a message is normally removed the moment it has been read by
# everyone it was addressed to (see _settle_delivery). The TTL only
# catches mail nobody ever looked at.
#
# It was sixty seconds when delivery could only ride on a hook the agent
# happened to fire, which made anything older than a minute misleading. A
# turn can now be woken at its end, so the window in which a message is
# still worth acting on is wider, and an unread one is worth keeping for
# more than a minute.
#
# The cost is still real. A window that is idle at its prompt runs no
# hooks, so mail addressed to it waits for the operator either way; ten
# minutes only widens the odds that something fires first. Age is
# rendered on every message so a reader can judge how stale it is.
MESSAGE_TTL_SECONDS = 600

# Compaction rewrites the file without its expired lines. Triggered by size
# rather than on a timer, because there is no daemon to run a timer.
COMPACT_BYTES = 1024 * 1024

# Presence is a file whose mtime is the heartbeat. Agents get killed and
# Ctrl-C'd constantly; anything relying on a clean shutdown to clear
# presence would show ghosts forever.
PRESENCE_TTL_SECONDS = 120

# Nothing clears a presence file on exit, so every window that ever ran
# stays on the list forever. Past this idle time the row is deleted
# outright along with the session's cursor, job and handle. Kept well
# above PRESENCE_TTL_SECONDS because presence only refreshes when a tool
# call fires a hook: a window sitting idle at a prompt is still alive,
# and one that comes back after a reap simply writes its files again.
PRESENCE_REAP_SECONDS = 3600

# "message" is conversation, "task" is a delegation that expects a "result"
# carrying the same task_id back, and "ack" is the receipt the bus posts
# by itself when a message is handed to its recipient.
MESSAGE_KINDS = ("message", "task", "result", "ack")

# The job recorded on a message meant for everyone. Delivery no longer
# reads the job at all, so this is now only a label saying the message
# was not about one piece of work.
BROADCAST_JOB = "*"

# Agent names become filename fragments and argv items, so they are
# restricted rather than escaped.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The CLIs a hook or MCP server can be running underneath. Finding one of
# these among our ancestors is what identifies the window we belong to.
CLI_NAMES = ("claude", "codex", "gemini", "node")


class Bus(object):
    """Paths and identity for one process talking to the bus.

    Exists so callers keep the shape they had when this was a Redis
    client: every function takes it as its first argument.
    """

    def __init__(self, directory=None, session=None, cwd=None):
        """Create the bus directory if this is the first process to arrive.

        Args:
            directory: Bus directory; defaults to /tmp/agentbus.
            session: Explicit session id. A hook is handed one by its CLI
                and should pass it, because guessing from the process tree
                is wrong under Codex -- see _session_key.
            cwd: The session's working directory. A hook is handed this
                too, and must pass it: the hook process runs wherever the
                CLI happened to spawn it, which is not necessarily where
                the session is working, and the job is derived from it.
        """
        self.directory = directory or BUS_DIR
        self.path = os.path.join(self.directory, "bus.jsonl")
        self.state = os.path.join(self.directory, "state")
        os.makedirs(self.state, exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a").close()
        self.session = session or _session_key()
        # An explicit job set by this session wins over the guess from cwd,
        # so two windows in one repo can split into separate conversations.
        self.cwd = os.path.abspath(cwd or os.getcwd())
        declared = (os.environ.get("AGENTBUS_JOB")
                    or _read_job(self.state, self.session, self.cwd))
        # Guessed from the directory when the session has not said. It is
        # a label on the roster either way, so a wrong guess costs nothing
        # more than a misleading line.
        self.job = declared or default_job(self.cwd)
        # Every session publishes a handle of its own on the roster.
        # "codex" is not an address when three windows answer to it, so a
        # sender that means one particular window has a name to use.
        self.handle = None


def default_handle(agent, session):
    """The name a session publishes if it does not choose one."""
    return "%s-%s" % (agent, session[-4:].lower())


def _handle_path(state, session):
    """Where a session's chosen published name is remembered."""
    return os.path.join(state, "handle.%s" % session)


def _read_handle(state, session):
    """Return the name a session published earlier in its life, if any."""
    try:
        with open(_handle_path(state, session)) as handle:
            return handle.read().strip() or None
    except (IOError, OSError):
        return None


def current_handle(bus, agent):
    """The name this session publishes right now.

    Args:
        bus: Bus from connect().
        agent: The CLI name, used only to build the default.

    Returns:
        The chosen name, or the generated one if none was chosen.
    """
    return (bus.handle or _read_handle(bus.state, bus.session)
            or default_handle(agent, bus.session))


def _name_conflict(bus, handle):
    """Say why a name cannot be taken, or None if it is free.

    Two windows answering to one name is worse than no name at all:
    delivery matches on the handle, so both would receive mail meant for
    one of them, and the roster would offer the sender no way to tell
    them apart. A name a dead session left behind is free -- only live
    windows hold one.

    Args:
        bus: Bus from connect().
        handle: The name being claimed.

    Returns:
        A sentence naming the holder, or None.
    """
    for row in agents(bus):
        if handle == row["name"]:
            return ("%r is the CLI address for every %s window; mail sent "
                    "to it reaches all of them" % (handle, row["name"]))
        if not row["online"]:
            continue
        if row.get("session") == bus.session:
            continue
        if handle == row["handle"]:
            return ("%r is already published by a live session working on "
                    "%s" % (handle, row.get("job", "?")))
    return None


def set_name(bus, handle):
    """Publish a name for this session on the roster.

    Addressing an agent by CLI name reaches every window running it,
    which is right for "any codex will do" and wrong for "the codex that
    is already looking at this file". A handle makes the second possible,
    so it is worth naming a window after the task it is on.

    Args:
        bus: Bus from connect().
        handle: The name to publish, e.g. "codex-sso".

    Returns:
        The handle now in effect.

    Raises:
        ValueError: The name is malformed, or another live session or a
            CLI already answers to it.
    """
    check_name(handle)
    conflict = _name_conflict(bus, handle)
    if conflict:
        raise ValueError("%s -- pick another" % conflict)
    with open(_handle_path(bus.state, bus.session), "w") as target:
        target.write(handle)
    bus.handle = handle
    _republish_handle(bus, handle)
    return handle


def _republish_handle(bus, handle):
    """Write a new name into this session's presence rows at once.

    The roster reads the name out of the presence file, which is only
    rewritten on the next heartbeat. Without this, a window that has
    just renamed itself still shows its old name to everyone deciding
    whom to write to -- including the next window checking whether the
    name is free.
    """
    tail = "." + bus.session
    for name in os.listdir(bus.state):
        if not name.startswith("presence.") or not name.endswith(tail):
            continue
        path = os.path.join(bus.state, name)
        try:
            with open(path) as source:
                record = json.load(source)
        except (IOError, OSError, ValueError):
            continue
        record["handle"] = handle
        temporary = "%s.%d.tmp" % (path, os.getpid())
        with open(temporary, "w") as target:
            json.dump(record, target)
        os.replace(temporary, path)


def _job_path(state, session):
    """Where a session's chosen job name is remembered."""
    return os.path.join(state, "job.%s" % session)


def _job_cwd_path(state, cwd):
    """Where the job last declared for a directory is remembered.

    An MCP server identifies its session by the CLI's process id, which
    changes whenever the CLI restarts -- orphaning the job that session
    declared and making it refuse to send until someone declares it
    again. A directory is the stabler thing: it is what the job is
    really about, so a new session working there inherits the
    declaration instead of nagging.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", os.path.abspath(cwd).lower()).strip("_")
    return os.path.join(state, "jobcwd.%s" % (slug or "root"))


def _read_file(path):
    """Read a small state file, or None when it is not there."""
    try:
        with open(path) as handle:
            return handle.read().strip() or None
    except (IOError, OSError):
        return None


def _read_job(state, session, cwd=None):
    """Return the job in force: this session's, else this directory's."""
    declared = _read_file(_job_path(state, session))
    if declared:
        return declared
    return _read_file(_job_cwd_path(state, cwd or os.getcwd()))


def set_job(bus, job):
    """Declare what this session is working on.

    A label for the roster, so another window can see what this one is
    busy with before interrupting it. It does not gate delivery; every
    session on the machine is reachable from every other.
    """
    job = (job or "").strip()
    if not job:
        raise ValueError("job name is required")
    for path in (_job_path(bus.state, bus.session),
                 _job_cwd_path(bus.state, bus.cwd)):
        with open(path, "w") as handle:
            handle.write(job)
    bus.job = job
    return job


def connect(directory=None, session=None, cwd=None):
    """Open the bus for this process."""
    return Bus(directory, session, cwd)


def check_name(name):
    """Reject anything unsafe as a filename fragment or argv item."""
    if not name or not NAME_PATTERN.match(name):
        raise ValueError(
            "invalid agent name %r: use lowercase letters, digits, '-' and "
            "'_', max 32 chars" % name)
    return name


def _process_name(pid):
    """Read a process's command name, or None if it has gone."""
    try:
        with open("/proc/%d/comm" % pid) as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return None


def _parent_of(pid):
    """Read a process's parent pid, or None if it has gone."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except (IOError, OSError, IndexError):
        return None
    return int(fields[1])


def _git_branch(directory):
    """Read the checked-out branch without shelling out to git."""
    head = os.path.join(directory, ".git", "HEAD")
    try:
        with open(head) as handle:
            text = handle.read().strip()
    except (IOError, OSError):
        return None
    if text.startswith("ref: refs/heads/"):
        return text.split("refs/heads/", 1)[1]
    return None


def default_job(directory=None):
    """Name the piece of work a session is on, from where it is sitting.

    Two windows open on the same repository and branch are working on the
    same thing and should hear each other; a window sitting somewhere else
    is doing something unrelated and should not be interrupted by it. The
    directory and branch are the cheapest honest approximation of that,
    and a session can always override it with set_job.
    """
    directory = os.path.abspath(directory or os.getcwd())
    walk = directory
    while walk != "/":
        branch = _git_branch(walk)
        if branch:
            return "%s@%s" % (os.path.basename(walk), branch)
        walk = os.path.dirname(walk)
    return os.path.basename(directory) or "home"


def _is_shared_daemon(pid):
    """Is this a process every window of a CLI has in common?

    Codex's `app-server` daemon is the parent of MCP servers across all
    Codex windows. Treating it as a session identity collapses them into
    one reader.
    """
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            cmdline = handle.read().decode(errors="replace")
    except (IOError, OSError):
        return False
    return "app-server" in cmdline or "--managed-daemon" in cmdline


def _session_key():
    """Identify the CLI window this process belongs to.

    Walks up the process tree to the nearest claude/codex/gemini ancestor.
    The MCP server and the session hooks of one window find the same
    process, so they share a read position and a message is not shown
    twice; two windows of the same CLI find different ones and stay
    independent.

    This is a fallback, and under Codex it is not good enough: Codex runs
    MCP servers under one shared `codex app-server` daemon, so every
    window resolves to the same pid and they steal each other's mail --
    the exact failure the file was meant to end. Anything holding a real
    session id from its CLI should pass it to connect() instead.
    """
    pid = os.getpid()
    for _step in range(12):
        parent = _parent_of(pid)
        if not parent or parent == pid:
            break
        name = _process_name(parent)
        if name in CLI_NAMES:
            if not _is_shared_daemon(parent):
                return "%s%d" % (name, parent)
            # Spawned by the daemon every window shares. Falling through
            # to os.getppid() would hand back that same daemon, which is
            # how the shared-cursor bug survived its first fix. Our own
            # pid is the only per-window identity available, and it is
            # stable because one such server lives per window.
            return "pid%d" % os.getpid()
        pid = parent
    return "pid%d" % os.getppid()


def _append(bus, record):
    """Append one message under an exclusive lock.

    The lock matters: three CLIs write to this file and a torn line would
    be unparseable for every reader, permanently.
    """
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(bus.path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _compact(bus):
    """Rewrite the file without spent or expired messages, if it is large.

    Cursors are byte offsets, so a rewrite moves every reader's position.
    They are reset to the new end of file rather than rescanning: the
    messages dropped were expired anyway, and the alternative is
    re-delivering an hour of old traffic to everyone at once.
    """
    try:
        if os.path.getsize(bus.path) < COMPACT_BYTES:
            return
    except OSError:
        return

    cutoff = time.time() - MESSAGE_TTL_SECONDS
    consumed = consumed_ids(bus)
    kept = []
    with open(bus.path) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("id") in consumed:
                    continue
                if record.get("ts", 0) >= cutoff:
                    kept.append(line)
            with open(bus.path + ".tmp", "w") as out:
                out.writelines(kept)
            os.replace(bus.path + ".tmp", bus.path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    end = os.path.getsize(bus.path)
    for name in os.listdir(bus.state):
        if name.startswith("cursor."):
            _write_cursor_path(os.path.join(bus.state, name), end)


def _delivery_path(bus):
    """Where the record of who has read what is kept."""
    return os.path.join(bus.state, "delivered.json")


def _live_addressees(bus, to):
    """Sessions that are online now and answer to this address.

    "Online" is the same test the roster shows -- a heartbeat inside
    PRESENCE_TTL_SECONDS -- so what the bus counts as an addressee is
    what `bmail` prints, rather than a second private notion of alive.

    The consequence is worth stating plainly: a window that has been
    silent longer than that is not counted, so mail can be consumed
    without it ever seeing it. Presence only refreshes when a hook fires,
    and a window sitting at an empty prompt fires none.
    """
    now = time.time()
    live = set()
    for name in os.listdir(bus.state):
        if not name.startswith("presence."):
            continue
        try:
            with open(os.path.join(bus.state, name)) as handle:
                record = json.load(handle)
        except (IOError, OSError, ValueError):
            continue
        if now - record.get("last_seen", 0) >= PRESENCE_TTL_SECONDS:
            continue
        session = record.get("session")
        if not session:
            continue
        agent = record.get("agent", "")
        handle_name = record.get("handle") or default_handle(agent, session)
        if to in (agent, handle_name):
            live.add(session)
    return live


def _load_delivery(bus):
    """Read the ledger, tolerating it not existing yet."""
    try:
        with open(_delivery_path(bus)) as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return {}


def consumed_ids(bus):
    """Message ids that have been read by everyone they were sent to."""
    return set(key for key, entry in _load_delivery(bus).items()
               if entry.get("done"))


def _settle_delivery(bus, messages):
    """Note that this session has read these, and drop the covered ones.

    A message addressed to a CLI name reaches every window running it, so
    one window reading it is not the end of its life -- that was the
    consumer-group behaviour this bus exists to avoid. It is finished
    once every session that is live *now* and answers to the address has
    read it, which for a message sent to one window's handle is the
    moment that window looks.

    Held under a lock for the whole read-modify-write: three CLIs settle
    into this file and a lost update would strand a message as
    permanently half-read.

    Returns:
        The ids tombstoned by this call.
    """
    if not messages:
        return []

    finished = []
    path = _delivery_path(bus)
    lock = path + ".lock"
    with open(lock, "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            ledger = _load_delivery(bus)
            cutoff = time.time() - MESSAGE_TTL_SECONDS
            # Entries outlive the message they describe by nothing: once
            # the line is past its TTL it can never be delivered again,
            # so its bookkeeping is dead weight.
            ledger = dict((key, entry) for key, entry in ledger.items()
                          if entry.get("ts", 0) >= cutoff)

            for message in messages:
                key = message.get("id")
                if not key:
                    continue
                entry = ledger.setdefault(key, {"ts": message.get("ts", 0),
                                                "readers": [],
                                                "done": False})
                if bus.session not in entry["readers"]:
                    entry["readers"].append(bus.session)
                if entry.get("done"):
                    continue
                waiting = _live_addressees(bus, message.get("to", ""))
                if waiting <= set(entry["readers"]):
                    entry["done"] = True
                    finished.append(key)

            temporary = "%s.%d.tmp" % (path, os.getpid())
            with open(temporary, "w") as out:
                json.dump(ledger, out)
            os.replace(temporary, path)
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)
    return finished


def _cursor_path(bus, agent):
    """Where this reader's position in the file is kept."""
    return os.path.join(bus.state, "cursor.%s.%s" % (agent, bus.session))


def _read_cursor(path):
    """Return a saved byte offset, or 0 when this reader is new."""
    try:
        with open(path) as handle:
            return int(handle.read().strip() or 0)
    except (IOError, OSError, ValueError):
        return 0


def _write_cursor_path(path, offset):
    """Save a byte offset, replacing the file atomically."""
    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w") as handle:
        handle.write(str(offset))
    os.replace(temporary, path)


def send(bus, sender, to, text, kind="message", task_id=None,
         reply_to=None, job=None):
    """Append a message addressed to another agent.

    Args:
        bus: Bus from connect().
        sender: Name of the sending agent.
        to: Name of the receiving agent.
        text: Message body, treated as data and never executed.
        kind: One of MESSAGE_KINDS.
        task_id: Task this message belongs to, for task/result pairs.
        reply_to: Id of the message being answered, if any.
        job: The piece of work this belongs to, recorded on the message
            as a label. Defaults to the sender's own job. It does not
            affect who receives the message.

    Returns:
        The id of the appended message.
    """
    check_name(sender)
    check_name(to)
    if kind not in MESSAGE_KINDS:
        raise ValueError("invalid kind %r: expected one of %s"
                         % (kind, ", ".join(MESSAGE_KINDS)))
    if not text or not text.strip():
        raise ValueError("message text is required")
    record = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "from": sender,
              "to": to, "kind": kind, "text": text,
              "job": job or bus.job,
              "from_handle": current_handle(bus, sender)}
    if task_id:
        record["task_id"] = task_id
    if reply_to:
        record["reply_to"] = reply_to

    _append(bus, record)
    _compact(bus)
    return record["id"]


def _scan(bus, offset):
    """Read complete lines from an offset.

    Returns:
        A (entries, end_offset) pair, where each entry is a
        (record, offset_just_past_it) tuple. A trailing partial line,
        which can exist while a writer is mid-append, is left for the
        next read.
    """
    entries = []
    try:
        with open(bus.path) as handle:
            handle.seek(offset)
            data = handle.read()
            position = offset + len(data.encode("utf-8"))
    except (IOError, OSError):
        return [], offset

    if data and not data.endswith("\n"):
        partial = data.rsplit("\n", 1)[-1]
        data = data[:len(data) - len(partial)]
        position -= len(partial.encode("utf-8"))

    # Each record carries the offset just past it, so a caller that stops
    # early can leave the cursor exactly there.
    walk = offset
    for line in data.splitlines(True):
        walk += len(line.encode("utf-8"))
        try:
            entries.append((json.loads(line), walk))
        except ValueError:
            continue
    return entries, position


def _for_me(bus, agent, record, cutoff, consumed=()):
    """Is this message addressed to this session, still fresh, and unspent?

    The address matches either the CLI name, which reaches every window
    running it, or this session's published handle, which reaches only
    this one.

    Every session on the machine can reach every other. The job used to
    partition delivery, and the partition was invisible: a window sent
    into silence and a window that had nothing to say looked the same
    from either side. The job stays on the roster as a label saying what
    each window is working on, which is the part that was ever useful.

    A message leaves the bus two ways: every session it was addressed to
    has read it, or it passed its TTL unread. `consumed` carries the
    first of those -- the caller loads it once per scan rather than this
    function re-reading the ledger for every line.
    """
    handle = current_handle(bus, agent)
    if record.get("to") not in (agent, handle):
        return False
    if record.get("id") in consumed:
        return False
    return record.get("ts", 0) >= cutoff


def receive(bus, agent, limit=10, block_ms=0, redelivered_first=False):
    """Collect messages addressed to this agent since its last read.

    Args:
        bus: Bus from connect().
        agent: Agent reading its own mail.
        limit: Maximum messages to return.
        block_ms: How long to wait when nothing new is there.
        redelivered_first: Accepted and ignored; kept so callers written
            against the queue version keep working. A log has no
            redelivery -- a reader's position is its own.

    Returns:
        List of message dicts, oldest first, at most `limit` of them. Any
        beyond the limit stay unread for the next call.
    """
    check_name(agent)
    touch(bus, agent)
    path = _cursor_path(bus, agent)
    deadline = time.time() + (block_ms / 1000.0)

    while True:
        entries, position = _scan(bus, _read_cursor(path))
        cutoff = time.time() - MESSAGE_TTL_SECONDS
        consumed = consumed_ids(bus)
        mine = []
        # Advance only past what is actually handed over. Moving the
        # cursor to the end of the scan while returning a truncated slice
        # silently destroyed every message the limit withheld.
        advance = position
        for record, end in entries:
            if not _for_me(bus, agent, record, cutoff, consumed):
                continue
            mine.append(record)
            if len(mine) >= limit:
                advance = end
                break
        _write_cursor_path(path, advance)
        if mine:
            # Handing them over is the read. Record it now, so a message
            # every addressee has seen stops being one the bus carries.
            _settle_delivery(bus, mine)
        if mine or time.time() >= deadline:
            return mine
        time.sleep(0.25)


def receive_and_settle(bus, agent, limit=10, block_ms=0, fresh_only=False):
    """Collect messages, acknowledge them, and record any tasks among them.

    Delivery here is the only moment the bus knows a message reached a
    model rather than merely being written down, so the receipt goes out
    now. Without it a sender cannot tell "nobody has looked yet" from
    "seen and ignored". It is also what spends the message: a read by
    the last session it was addressed to takes it off the bus.

    Task bookkeeping is still done separately, so the agent that
    delegated can ask whether the work was picked up.
    """
    messages = receive(bus, agent, limit=limit, block_ms=block_ms)
    for message in messages:
        if message.get("kind") == "task" and message.get("task_id"):
            record_task(bus, message["task_id"], status="delivered")
    post_receipts(bus, agent, messages)
    return messages


def ack(bus, agent, message_ids):
    """Accepted for compatibility; a log needs no acknowledgement."""
    return len(message_ids or [])


def post_receipts(bus, agent, messages):
    """Post a receipt back to the sender of each delivered message.

    An ack is never acknowledged, or two windows reading each other would
    trade receipts forever. It carries the job of the message it answers
    rather than the reader's own, because the sender is by definition on
    that job and may not be on the reader's.

    Args:
        bus: Bus from connect().
        agent: The agent doing the acknowledging.
        messages: The message records just delivered.

    Returns:
        The number of receipts posted.
    """
    posted = 0
    for message in messages or []:
        if message.get("kind") == "ack":
            continue
        # Addressed to the sender's handle, not its CLI name: the receipt
        # belongs to the window that sent, not to every window running it.
        sender = message.get("from_handle") or message.get("from")
        if not sender:
            continue
        send(bus, agent, sender, "receipt", kind="ack",
             reply_to=message.get("id"), job=message.get("job"))
        posted += 1
    return posted


def _presence_path(bus, agent):
    """Where this session's heartbeat file lives.

    Keyed by session as well as agent: two Codex windows are two
    participants on the bus, possibly on different jobs, and collapsing
    them into one row hid exactly the case that matters.
    """
    return os.path.join(bus.state, "presence.%s.%s" % (agent, bus.session))


def touch(bus, agent, status=None, **info):
    """Refresh an agent's presence, optionally changing its status."""
    check_name(agent)
    path = _presence_path(bus, agent)
    record = {}
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (IOError, OSError, ValueError):
        record = {}

    record["last_seen"] = time.time()
    record["host"] = socket.gethostname()
    record["session"] = bus.session
    record["job"] = bus.job
    record["agent"] = agent
    record["handle"] = current_handle(bus, agent)
    if status:
        record["status"] = status
    for name, value in info.items():
        if value is not None:
            record[name] = str(value)

    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w") as handle:
        json.dump(record, handle)
    os.replace(temporary, path)
    return agent


def register(bus, agent, **info):
    """Announce an agent when its session starts."""
    check_name(agent)
    status = info.pop("status", "idle")
    defaults = {"pid": os.getpid(), "cwd": os.getcwd()}
    defaults.update(info)
    return touch(bus, agent, status=status, **defaults)


def _discard(bus, name):
    """Delete one state file, tolerating it already being gone."""
    try:
        os.unlink(os.path.join(bus.state, name))
    except OSError:
        pass


def _reap_orphans(bus, alive):
    """Delete per-session state belonging to no surviving presence file.

    job and handle are keyed by session alone, so they outlive the
    presence row that named the agent. They are only removed once no
    presence file mentions the session at all, because a window can
    rename itself and leave a second presence file under the old name.

    Args:
        bus: The bus whose state directory is being swept.
        alive: Set of session ids that still have a presence file.
    """
    for name in os.listdir(bus.state):
        parts = name.split(".", 1)
        if parts[0] not in ("job", "handle"):
            continue
        if len(parts) < 2:
            continue
        if parts[1] in alive:
            continue
        _discard(bus, name)


def agents(bus):
    """List every agent the bus has seen, online or not.

    Sessions idle past PRESENCE_REAP_SECONDS are deleted rather than
    listed. There is no daemon to run a timer, so the sweep rides on this
    call -- the one place that already walks the whole state directory.

    Returns:
        List of dicts with name, status, online, unread and the recorded
        presence fields, sorted by name.
    """
    now = time.time()
    rows = []
    alive = set()
    for name in sorted(os.listdir(bus.state)):
        if not name.startswith("presence."):
            continue
        parts = name.split(".", 2)
        if len(parts) < 3:
            continue
        agent = parts[1]
        session = parts[2]
        try:
            with open(os.path.join(bus.state, name)) as handle:
                record = json.load(handle)
        except (IOError, OSError, ValueError):
            continue
        idle = now - record.get("last_seen", 0)
        if idle >= PRESENCE_REAP_SECONDS:
            _discard(bus, name)
            _discard(bus, "cursor.%s.%s" % (agent, session))
            continue
        alive.add(session)
        row = dict(record)
        row["name"] = agent
        row["online"] = idle < PRESENCE_TTL_SECONDS
        row["status"] = record.get("status", "idle") if row["online"] else "offline"
        row["idle_seconds"] = round(idle, 1)
        row["job"] = record.get("job", "?")
        row["handle"] = record.get("handle",
                                   default_handle(agent, record.get("session", "")))
        row["unread"] = unread_count(bus, agent, record.get("session"))
        row["pending"] = 0
        rows.append(row)
    _reap_orphans(bus, alive)
    return rows


def unread_count(bus, agent, session=None):
    """Count messages an agent session has not read yet.

    The session must be the one being asked about. Counting against the
    caller's own cursor reports "messages since I last looked", which is
    a different and useless number.
    """
    path = os.path.join(bus.state, "cursor.%s.%s"
                        % (agent, session or bus.session))
    entries, _position = _scan(bus, _read_cursor(path))
    cutoff = time.time() - MESSAGE_TTL_SECONDS
    consumed = consumed_ids(bus)
    return len([r for r, _end in entries
                if _for_me(bus, agent, r, cutoff, consumed)])


def _load_tasks(bus):
    """Read the task ledger, tolerating it not existing yet."""
    try:
        with open(os.path.join(bus.state, "tasks.json")) as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return {}


def record_task(bus, task_id, **fields):
    """Store or update the record for a delegated task."""
    path = os.path.join(bus.state, "tasks.json")
    with open(path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            tasks = _load_tasks(bus)
            entry = tasks.setdefault(task_id, {})
            entry.update({k: v for k, v in fields.items() if v is not None})
            entry["updated"] = time.time()
            temporary = "%s.%d.tmp" % (path, os.getpid())
            with open(temporary, "w") as handle:
                json.dump(tasks, handle)
            os.replace(temporary, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return task_id


def get_task(bus, task_id):
    """Return a task record, or an empty dict if unknown."""
    return _load_tasks(bus).get(task_id, {})


def new_task_id():
    """Mint an id tying a delegated task to the result that answers it."""
    return "task_%s" % uuid.uuid4().hex[:10]


def format_messages(messages):
    """Render messages as plain text for injection into a session.

    Age is on every line so a reader can see at a glance whether a message
    belongs to what is happening now.
    """
    lines = []
    for message in messages:
        sender = message.get("from_handle") or message.get("from", "?")
        age = int(time.time() - message.get("ts", time.time()))
        # A receipt has no body worth printing: who read what, and when.
        if message.get("kind") == "ack":
            lines.append("[ack] %s read your message %s (%ds ago)"
                         % (sender, message.get("reply_to", "?"),
                            max(age, 0)))
            lines.append("")
            continue
        header = "[%s] from %s" % (message.get("kind", "message"), sender)
        if message.get("task_id"):
            header += " (%s)" % message["task_id"]
        header += " id=%s" % message.get("id", "?")
        header += " (%ds ago)" % max(age, 0)
        lines.append(header)
        lines.append(message.get("text", ""))
        lines.append("")
    return "\n".join(lines).strip()


def watch(bus, on_message):
    """Follow every message crossing the bus without consuming any of it.

    Tails the file from its end. It keeps no cursor of its own, so
    watching never affects what an agent receives.
    """
    offset = os.path.getsize(bus.path)
    while True:
        entries, offset = _scan(bus, offset)
        for record, _end in entries:
            on_message(record)
        time.sleep(0.4)


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
        bus: The bus, for the session id the handles are looked up under.
        name: The name supplied as the caller's own identity.

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
            with open(os.path.join(bus.state, entry)) as opened:
                record = json.load(opened)
        except (IOError, OSError, ValueError):
            record = {}
        published = record.get("handle") or default_handle(agent, bus.session)
        known[published] = agent
    if name in known.values():
        return name
    return known.get(name, name)


def _main(argv):
    """Shell access to the bus, for testing and for scripts without MCP."""
    bus = connect()
    command = argv[0] if argv else "agents"

    if command == "agents":
        for row in agents(bus):
            print("%-14s %-8s %-9s job=%-22s unread=%-3s"
                  % (row["handle"], row["name"],
                     "online" if row["online"] else "offline",
                     row["job"], row["unread"]))
        return 0

    if command == "send":
        try:
            print(send(bus, _as_agent(bus, argv[1]), argv[2],
                       " ".join(argv[3:])))
        except ValueError as error:
            print("error: %s" % error)
            return 1
        return 0

    if command == "broadcast":
        print(send(bus, _as_agent(bus, argv[1]), argv[2],
                   " ".join(argv[3:]), job=BROADCAST_JOB))
        return 0

    if command in ("read", "drain"):
        print(format_messages(
            receive_and_settle(bus, _as_agent(bus, argv[1]), limit=50))
              or "(no messages)")
        return 0

    if command == "watch":
        def _print(message):
            """Print one line per message as it crosses the bus."""
            print("%s  %s -> %s  [%s]  %s"
                  % (time.strftime("%H:%M:%S"), message.get("from", "?"),
                     message.get("to", "?"), message.get("kind", "message"),
                     message.get("text", "").replace("\n", " ")[:160]),
                  flush=True)

        print("watching %s (ctrl-c to stop)" % bus.path, flush=True)
        watch(bus, _print)
        return 0

    if command == "register":
        print(register(bus, argv[1]))
        return 0

    if command == "name":
        if len(argv) < 2:
            print(_read_handle(bus.state, bus.session) or "(none set)")
            return 0
        # A refused name is an ordinary outcome -- the caller picks
        # another -- so it prints a line rather than a traceback.
        try:
            print(set_name(bus, argv[1]))
        except ValueError as error:
            print("cannot take that name: %s" % error, file=sys.stderr)
            return 1
        return 0

    if command == "job":
        if len(argv) > 1:
            print(set_job(bus, " ".join(argv[1:])))
        else:
            print(bus.job)
        return 0

    if command == "path":
        print(bus.path)
        return 0

    print("usage: bus.py agents|watch|path|job [name]|name [handle]|"
          "send <from> <to> <text>|broadcast <from> <to> <text>|"
          "read <agent>|register <agent>")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
