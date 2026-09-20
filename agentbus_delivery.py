"""Deliver each window its own mail and settle only the copies it has read."""

import fcntl
import json
import os
import time

import agentbus_constants as constants
import agentbus_identity as identity
import agentbus_messages as messaging
import agentbus_presence as presence
import agentbus_routing as routing
import agentbus_state as state
import agentbus_storage as storage


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

    Args:
        bus (Bus): Connection whose shared bus state is used.
        messages (list[dict]): Message envelopes delivered to this window.

    Returns:
        The ids tombstoned by this call.
    """
    if not messages:
        return []

    finished = []
    path = storage.delivery_path(bus)
    lock = path + ".lock"
    with open(lock, "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            ledger = storage.load_delivery(bus)
            cutoff = time.time() - constants.MESSAGE_TTL_SECONDS
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
                if message.get("to_session"):
                    waiting = {
                        identity.canonical_session(
                            bus,
                            message["to_session"],
                            message.get("to_session_start"))}
                else:
                    waiting = presence.live_addressees(
                        bus, message.get("to", ""))
                if waiting <= set(entry["readers"]):
                    entry["done"] = True
                    finished.append(key)

            temporary = f'{path!s}.{os.getpid():d}.tmp'
            with open(temporary, "w", encoding="utf-8") as out:
                json.dump(ledger, out)
            os.replace(temporary, path)
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)
    return finished


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

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        record (dict): Message or state record to inspect or persist.
        cutoff (float): Oldest acceptable message timestamp in seconds.
        consumed (set[str] or tuple): Ids already read or completely settled.

    Returns:
        bool: Whether this window can still receive the envelope.
    """
    if not routing.addressed_to(bus, agent, record):
        return False
    if record.get("id") in consumed:
        return False
    return record.get("ts", 0) >= cutoff


def receive(bus, agent, limit=10, block_ms=0, redelivered_first=False):
    """Collect messages addressed to this agent since its last read.

    Args:
        bus (Bus): Bus from connect().
        agent (str): Agent reading its own mail.
        limit (int): Maximum messages to return.
        block_ms (int): How long to wait when nothing new is there.
        redelivered_first (bool): Accepted and ignored; kept so callers written
            against the queue version keep working. A log has no
            redelivery -- a reader's position is its own.

    Returns:
        List of message dicts, oldest first, at most `limit` of them. Any
        beyond the limit stay unread for the next call.
    """
    del redelivered_first
    state.check_name(agent)
    presence.touch(bus, agent)
    path = storage.cursor_path(bus, agent)
    deadline = time.time() + (block_ms / 1000.0)

    while True:
        entries, position = storage.scan(bus, storage.read_cursor(path))
        cutoff = time.time() - constants.MESSAGE_TTL_SECONDS
        consumed = storage.seen_ids(bus)
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
        storage.write_cursor_path(path, advance)
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

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        limit (int): Maximum number of envelopes returned by this read.
        block_ms (int): Maximum wait for new mail, in milliseconds.
        fresh_only (bool): Compatibility option; log reads already return
            unseen mail.

    Returns:
        list[dict]: Delivered envelopes with receipts posted.
    """
    del fresh_only
    messages = receive(bus, agent, limit=limit, block_ms=block_ms)
    for message in messages:
        if message.get("kind") == "task" and message.get("task_id"):
            storage.record_task(bus, message["task_id"], status="delivered")
    post_receipts(bus, agent, messages)
    return messages


def ack(bus, agent, message_ids):
    """Accepted for compatibility; a log needs no acknowledgement.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        message_ids (list[str]): Message ids acknowledged by the caller.

    Returns:
        int: Number of supplied ids acknowledged for compatibility.
    """
    del bus, agent
    return len(message_ids or [])


def post_receipts(bus, agent, messages):
    """Post a receipt back to the sender of each delivered message.

    An ack is never acknowledged, or two windows reading each other would
    trade receipts forever. It carries the job of the message it answers
    rather than the reader's own, because the sender is by definition on
    that job and may not be on the reader's.

    Args:
        bus (Bus): Bus from connect().
        agent (str): The agent doing the acknowledging.
        messages (list[dict]): The message records just delivered.

    Returns:
        The number of receipts posted.
    """
    posted = 0
    for message in messages or []:
        if message.get("kind") in ("ack", "project_check", "project_status"):
            continue
        # Addressed to the sender's handle, not its CLI name: the receipt
        # belongs to the window that sent, not to every window running it.
        sender = message.get("from_handle") or message.get("from")
        if not sender:
            continue
        try:
            messaging.send_direct(
                bus,
                agent,
                sender,
                "receipt",
                kind="ack",
                reply_to=message.get("id"),
                job=message.get("job"),
                reply_context=message)
        except ValueError:
            # A legacy sender may have no stable or unique identity. An
            # unroutable receipt must never hide the incoming message.
            continue
        posted += 1
    return posted


def peek(bus, agent, session=None):
    """The mail a session has waiting, without consuming any of it.

    Deliberately not a read. The cursor does not move and nothing is
    settled, so looking here cannot take a message away from the window
    it belongs to -- which is the whole requirement for the watcher, a
    process that must be able to see mail in order to ring about it and
    must never be the reader that spends it.

    The session must be the one being asked about. Scanning from the
    caller's own cursor answers "what has crossed the bus since I last
    looked", which is a different and useless question.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        session (str or None): Conversation identity; defaults to this window.

    Returns:
        list[dict]: Waiting mail without advancing the reader.
    """
    if session and session != bus.session:
        bus = bus.for_session(session)
    path = os.path.join(
        bus.state, (
            f'cursor.{agent!s}.{session or bus.session!s}'
        ))
    entries, _position = storage.scan(bus, storage.read_cursor(path))
    cutoff = time.time() - constants.MESSAGE_TTL_SECONDS
    consumed = storage.seen_ids(bus)
    return [r for r, _end in entries
            if _for_me(bus, agent, r, cutoff, consumed)]


def unread_count(bus, agent, session=None):
    """Count messages an agent session has not read yet.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        session (str or None): Conversation identity; defaults to this window.

    Returns:
        int: Number of waiting envelopes.
    """
    return len(peek(bus, agent, session))
