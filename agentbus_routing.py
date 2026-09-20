"""Resolve related windows while keeping explicit handles and replie."""

import collections

import agentbus_constants as constants
import agentbus_identity as identity
import agentbus_presence as presence
import agentbus_state as state
import agentbus_storage as storage

_RoutingOptions = collections.namedtuple(
    "RoutingOptions", 'reply_to task_id reply_context',
    defaults=(None, None, None))


def task_stem(handle, agent, session):
    """Task shared by codex-data-export-001 and claude-data-export-002.

    Args:
        handle (str): Published window name.
        agent (str): CLI name identifying this window on the bus.
        session (str or None): Conversation identity; defaults to this window.

    Returns:
        str or None: Task shared by handles from different CLIs.
    """
    if not handle or handle == state.default_handle(agent, session):
        return None
    stem = constants.NAME_NUMBER.sub("", handle)
    if stem == agent:
        return None
    if stem.startswith(agent + "-"):
        stem = stem[len(agent) + 1:]
    return stem or None


def addressed_to(bus, agent, record):
    """Match an envelope, including session-pinned sends and broadcasts.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        record (dict): Message or state record to inspect or persist.

    Returns:
        bool: Whether the envelope belongs to this inbox.
    """
    target = record.get("to")
    if record.get("broadcast"):
        return target in ("*", agent)
    if record.get("to_session"):
        return (
            identity.canonical_session(
                bus,
                record["to_session"],
                record.get("to_session_start")) == bus.session and record.get(
                "to_agent",
                agent) == agent)
    # Old envelopes and explicit handles for not-yet-registered windows.
    return target in (agent, state.current_handle(bus, agent))


def find_message(bus, message_id):
    """Find the original sender so a reply follows the same window.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        message_id (str): Message id used to identify an earlier envelope.

    Returns:
        dict or None: Matching envelope when still in the log.
    """
    for record, _end in storage.scan(bus, 0)[0]:
        if record.get("id") == message_id:
            return record
    return None


def _route_choice(requested, rows, reason):
    """Require one session, even if multiple rows publish the same name.

    Args:
        requested (str): Destination originally supplied by the sender.
        rows (list[dict]): Candidate windows from the shared roster.
        reason (str): Routing rule that selected these candidates.

    Returns:
        dict: Resolved window address and routing reason.
    """
    if len(rows) != 1:
        choices = ", ".join(
            (
                f'{
                    row['handle']!s} (job={
                    row.get(
                        'job',
                        '?')!s}, session=' f'{
                    row['session']!s})'
            ) for row in rows)
        raise ValueError(
            (
                f'ambiguous destination {requested!r}: {choices!s}. Use a '
                f'unique full handle, or give the windows distinct '
                f'jobs/names.'
            ))
    row = rows[0]
    return {"to": row["handle"], "to_session": row["session"],
            "to_agent": row["agent"], "routing": reason}


def _reply_original(client, sender, options):
    """Recover delegated requests even after their original log line expires.

    Args:
        client (Bus): Connection replying to an earlier envelope.
        sender (str): CLI sending the reply.
        options (RoutingOptions): Original message and optional task context.

    Returns:
        dict or None: Original envelope or reconstructed task routing fields.
    """
    original = options.reply_context or find_message(client, options.reply_to)
    if original is not None or not options.task_id:
        return original
    task = storage.get_task(client, options.task_id)
    if not task or task.get("message_id") != options.reply_to:
        return None
    return {
        "id": options.reply_to,
        "from": task.get("sender"),
        "from_handle": task.get("sender_handle"),
        "from_session": task.get("sender_session"),
        "from_session_start": task.get("sender_session_start"),
        "to": task.get("assignee_handle") or task.get("assignee"),
        "to_session": task.get("assignee_session"),
        "to_session_start": task.get("assignee_session_start"),
        "to_agent": task.get("assignee_agent") or sender,
    }


def _reply_destination(client, original, rows, requested, reply_to):
    """Keep replies pinned to the original sender after its handle changes.

    Args:
        client (Bus): Connection sending the reply.
        original (dict): Envelope being answered.
        rows (list[dict]): Current registered windows.
        requested (str): Destination requested by the caller.
        reply_to (str): Original message id used in routing errors.

    Returns:
        dict: Reply destination and its stable session identity.
    """
    target = original.get("from_handle") or original["from"]
    session = original.get("from_session")
    if session:
        session = identity.canonical_session(
            client, session, original.get("from_session_start"))
        current = [row for row in rows if row["session"] == session
                   and row["agent"] == original["from"]]
        if len(current) == 1:
            target = current[0]["handle"]
        return {"to": target, "to_session": session,
                "to_session_start": original.get("from_session_start"),
                "to_agent": original["from"], "routing": "reply"}
    matches = [row for row in rows if row["handle"] == target]
    if matches:
        return _route_choice(requested, matches, "reply")
    if target != original["from"]:
        return {"to": target, "routing": "reply"}
    raise ValueError(
        f"message {
            reply_to!r} has no window identity to reply to")


def _route_reply(client, sender, to, options, rows):
    """Validate reply ownership before ordinary task or job matching.

    Args:
        client (Bus): Connection sending the reply.
        sender (str): Replying CLI name.
        to (str): Requested reply destination.
        options (RoutingOptions): Original message and optional task context.
        rows (list[dict]): Current registered windows.

    Returns:
        dict or None: Reply route, or None to continue normal resolution.
    """
    original = _reply_original(client, sender, options)
    if original and to in (original.get("from"), original.get("from_handle")):
        if not addressed_to(client, sender, original):
            raise ValueError(
                f"message {
                    options.reply_to!r} was not addressed to this window")
        return _reply_destination(client, original, rows, to, options.reply_to)
    broad = to in constants.AGENT_NAMES or any(
        row["agent"] == to for row in rows)
    if broad and original and to != original.get("from"):
        raise ValueError(f"reply destination {to!r} does not match "
                         f"the original sender {original.get('from')!r}")
    if broad and original is None:
        raise ValueError(
            f"cannot find message {options.reply_to!r} to identify "
            "the reply window; use its full handle")
    return None


def resolve_recipient(bus, sender, to, *args, **kwargs):
    """Resolve a CLI destination to one related window without sending.

    Replies return to their original sender. Otherwise task names match first,
    then exact jobs. Full handles remain direct across jobs.

    Args:
        bus (Bus): Connection sending the message.
        sender (str): Sending CLI name.
        to (str): CLI name or full destination handle.
        *args (object): Optional reply_to, task_id, and reply_context values.
        **kwargs (object): Those same optional routing fields by name.

    Returns:
        dict: Destination handle, pinned session, and routing reason.
    """
    options = _RoutingOptions(*args, **kwargs)
    rows = presence.routing_rows(bus)
    if options.reply_to:
        reply = _route_reply(bus, sender, to, options, rows)
        if reply:
            return reply
    broad = to in constants.AGENT_NAMES or any(
        row["agent"] == to for row in rows)
    if not broad:
        matches = [row for row in rows if row["handle"] == to]
        return (_route_choice(to, matches, "handle") if matches
                else {"to": to, "routing": "handle"})
    candidates = [row for row in rows
                  if row["agent"] == to and row["session"] != bus.session]
    task = task_stem(state.current_handle(bus, sender), sender, bus.session)
    matches = [row for row in candidates if task and task_stem(
        row["handle"], to, row["session"]) == task]
    if matches:
        same_job = [row for row in matches if row.get("job") == bus.job]
        return _route_choice(to, same_job or matches, "task")
    matches = [row for row in candidates if row.get("job") == bus.job]
    if matches:
        return _route_choice(to, matches, "job")
    choices = ", ".join(f"{row['handle']} (job={row.get('job', '?')})"
                        for row in candidates) or "none registered"
    raise ValueError(
        f"no related {to} window for {state.current_handle(bus, sender)} "
        f"(job={bus.job}). Available: {choices}. "
        "Use a full handle or matching "
        "task names/jobs; use broadcast explicitly to reach every window.")
