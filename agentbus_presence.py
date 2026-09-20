"""Track window presence so names and recipient choices stay unambiguous."""

import json
import os
import socket
import time

import agentbus_constants as constants
import agentbus_state as state


def _name_conflict(bus, handle):
    """Say why a name cannot be taken, or None if it is free.

    Two windows answering to one name is worse than no name at all:
    delivery matches on the handle, so both would receive mail meant for
    one of them, and the roster would offer the sender no way to tell
    them apart. A name a dead session left behind is free -- only live
    windows hold one.

    Args:
        bus (Bus): Bus from connect().
        handle (str): The name being claimed.

    Returns:
        A sentence naming the holder, or None.
    """
    for row in roster(bus):
        if handle == row["name"]:
            return (
                (
                    f'{
                    handle!r} is the CLI address for every {
                    row['name']!s}' f' window; mail sent to it reaches all of them'
                ))
        if not row["online"]:
            continue
        if row.get("session") == bus.session:
            continue
        if handle == row["handle"]:
            return (
                (
                    f'{
                    handle!r} is already published by a live session ' f'working on {
                    row.get(
                    'job',
                    '?')!s}'
                ))
    return None


def _number_name(bus, stem):
    """Return the stem with the lowest free three-digit number on it.

    Two windows opened on one task both want to be called after it, and
    the useful answer is to say which is which rather than to refuse the
    second and make it invent a name that no longer describes the work.
    claude-sso-001 and claude-sso-002 are two addresses for one
    task, which is what the roster is being asked to show.

    The lowest free number is taken rather than the next one up, so the
    numbers a finished window frees come back into use and a task that
    runs all day does not count off into the hundreds.

    Args:
        bus (Bus): Bus from connect().
        stem (str): The name without a number, already checked.

    Returns:
        The numbered name.

    Raises:
        ValueError: Every number is held by a live session.
    """
    for number in range(1, constants.NAME_NUMBER_LIMIT):
        candidate = f'{stem!s}-{number:03d}'
        if _name_conflict(bus, candidate) is None:
            return candidate
    raise ValueError(
        (
            f'{
            stem!r} already has {
            constants.NAME_NUMBER_LIMIT -
            1:d} live ' f'windows on it -- that is not a naming problem'
        ))


def set_name(bus, handle):
    """Publish a name for this session on the roster.

    Addressing an agent by CLI name reaches every window running it,
    which is right for "any codex will do" and wrong for "the codex that
    is already looking at this file". A handle makes the second possible,
    so it is worth naming a window after the task it is on.

    The name is published with a three-digit number on the end:
    "codex-sso" becomes "codex-sso-001", and the next window on the same
    task becomes "codex-sso-002". Naming is then something a window can
    do without first looking to see who else is here, and the roster
    never carries two windows the sender cannot tell apart. A handle
    that already ends in a number is taken as meaning that particular
    window and is published as written, or refused if it is held.

    Args:
        bus (Bus): Bus from connect().
        handle (str): The name to publish, e.g. "codex-sso".

    Returns:
        The handle now in effect, numbered.

    Raises:
        ValueError: The name is malformed, too long to carry a number,
            or -- when it was written with one -- already answered to by
            another live session or a CLI.
    """
    state.check_name(handle)
    if constants.NAME_NUMBER.search(handle):
        conflict = _name_conflict(bus, handle)
        if conflict:
            raise ValueError(f'{conflict!s} -- pick another')
    else:
        if len(handle) > constants.NAME_STEM_MAX:
            raise ValueError(
                (
                    f'{
                    handle!r} leaves no room for the number on the end: ' f'use at most {
                    constants.NAME_STEM_MAX:d} characters'
                ))
        handle = _number_name(bus, handle)
    file_path = state.handle_path(bus.state, bus.session)
    with open(file_path, "w", encoding="utf-8") as target:
        target.write(handle)
    bus.handle = handle
    state.republish_handle(bus, handle)
    return handle


def live_addressees(bus, to):
    """Sessions that are online now and answer to this address.

    "Online" is the same test the roster shows -- a heartbeat inside
    PRESENCE_TTL_SECONDS -- so what the bus counts as an addressee is
    what `bmail` prints, rather than a second private notion of alive.

    The consequence is worth stating plainly: a window that has been
    silent longer than that is not counted, so mail can be consumed
    without it ever seeing it. Presence only refreshes when a hook fires,
    and a window sitting at an empty prompt fires none.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        to (str): CLI name, full handle, or explicit broadcast wildcard.

    Returns:
        set[str]: Online session ids matching the destination.
    """
    now = time.time()
    live = set()
    for name in os.listdir(bus.state):
        if not name.startswith("presence."):
            continue
        try:
            file_path = os.path.join(bus.state, name)
            with open(file_path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (IOError, OSError, ValueError):
            continue
        if now - record.get("last_seen", 0) >= constants.PRESENCE_TTL_SECONDS:
            continue
        session = record.get("session")
        if not session:
            continue
        agent = record.get("agent", "")
        handle_name = record.get(
            "handle") or state.default_handle(agent, session)
        if to == "*" or to in (agent, handle_name):
            live.add(session)
    return live


def routing_rows(bus):
    """Read registered windows without consuming mail or reaping state.

    A heartbeat older than two minutes means idle, not closed. Keep those
    windows eligible until the roster's normal one-hour reap threshold.

    Args:
        bus (Bus): Connection whose shared bus state is used.

    Returns:
        list[dict]: Registered windows still eligible for routing.
    """
    rows = []
    now = time.time()
    for name in sorted(os.listdir(bus.state)):
        if not name.startswith("presence."):
            continue
        try:
            file_path = os.path.join(bus.state, name)
            with open(file_path, encoding="utf-8") as handle:
                row = json.load(handle)
            if not isinstance(row, dict):
                continue
            if now - row.get("last_seen",
                             0) >= constants.PRESENCE_REAP_SECONDS:
                continue
        except (IOError, OSError, ValueError, TypeError):
            continue
        if not row.get("session") or not row.get("agent"):
            continue
        row["handle"] = (row.get("handle")
                         or state.default_handle(row["agent"], row["session"]))
        # A just-declared job is effective before the next hook heartbeat.
        row["job"] = (
            state.read_file(
                state.job_path(
                    bus.state,
                    row["session"])) or row.get("job"))
        rows.append(row)
    return rows


def _presence_path(bus, agent):
    """Where this session's heartbeat file lives.

    Keyed by session as well as agent: two Codex windows are two
    participants on the bus, possibly on different jobs, and collapsing
    them into one row hid exactly the case that matters.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.

    Returns:
        str: This window's heartbeat path.
    """
    return os.path.join(bus.state, f'presence.{agent!s}.{bus.session!s}')


def touch(bus, agent, status=None, **info):
    """Refresh an agent's presence, optionally changing its status.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        status (str or None): New roster status, or None to keep the old one.
        **info (object): Additional roster fields supplied by the integration.

    Returns:
        str: CLI name whose presence was refreshed.
    """
    state.check_name(agent)
    path = _presence_path(bus, agent)
    record = {}
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (IOError, OSError, ValueError):
        record = {}

    record["last_seen"] = time.time()
    record["host"] = socket.gethostname()
    record["session"] = bus.session
    record["job"] = bus.job
    record["agent"] = agent
    record["handle"] = state.current_handle(bus, agent)
    if status:
        record["status"] = status
    for name, value in info.items():
        if value is not None:
            record[name] = str(value)

    temporary = f'{path!s}.{os.getpid():d}.tmp'
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(temporary, path)
    return agent


def register(bus, agent, **info):
    """Announce an agent when its session starts.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
        **info (object): Additional roster fields supplied by the integration.

    Returns:
        str: Registered CLI name.
    """
    state.check_name(agent)
    status = info.pop("status", "idle")
    defaults = {"pid": os.getpid(), "cwd": os.getcwd()}
    defaults.update(info)
    return touch(bus, agent, status=status, **defaults)


def _reap_orphans(bus, alive):
    """Delete per-session state belonging to no surviving presence file.

    job and handle are keyed by session alone, so they outlive the
    presence row that named the agent. They are only removed once no
    presence file mentions the session at all, because a window can
    rename itself and leave a second presence file under the old name.

    Args:
        bus (Bus): The bus whose state directory is being swept.
        alive (set[str]): Set of session ids that still have a presence file.
    """
    for name in os.listdir(bus.state):
        parts = name.split(".", 1)
        if parts[0] not in ("job", "handle"):
            continue
        if len(parts) < 2:
            continue
        if parts[1] in alive:
            continue
        state.discard(bus, name)


def roster(bus):
    """List every agent the bus has seen, online or not.

    Sessions idle past PRESENCE_REAP_SECONDS are deleted rather than
    listed. There is no daemon to run a timer, so the sweep rides on this
    call -- the one place that already walks the whole state directory.

    Args:
        bus (Bus): Connection whose shared bus state is used.

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
            file_path = os.path.join(bus.state, name)
            with open(file_path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (IOError, OSError, ValueError):
            continue
        idle = now - record.get("last_seen", 0)
        if idle >= constants.PRESENCE_REAP_SECONDS:
            state.discard(bus, name)
            state.discard(bus, f'cursor.{agent!s}.{session!s}')
            continue
        alive.add(session)
        row = dict(record)
        row["name"] = agent
        row["online"] = idle < constants.PRESENCE_TTL_SECONDS
        row["status"] = record.get(
            "status", "idle") if row["online"] else "offline"
        row["idle_seconds"] = round(idle, 1)
        row["job"] = record.get("job", "?")
        row["handle"] = record.get(
            "handle", state.default_handle(
                agent, record.get(
                    "session", "")))
        row["pending"] = 0
        rows.append(row)
    _reap_orphans(bus, alive)
    return rows
