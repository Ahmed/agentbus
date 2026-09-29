"""Track window presence so names and recipient choices stay unambiguous."""

import fcntl
import itertools
import json
import os
import random
import re
import socket
import time

import agentbus_constants as constants
import agentbus_identity as identity
import agentbus_state as state


def _held_numbers(bus):
    """Numbers already standing for some other window.

    A name stays held while its window is on the roster at all, online or
    not, and for a window that has registered but not yet written a
    heartbeat. Handing a quiet window's name to a newcomer would send
    the newcomer mail meant for the window that went quiet.

    Args:
        bus (Bus): Bus from connect().

    Returns:
        set[str]: The part after the CLI in every other window's handle.
    """
    held = set()
    handles = [row["handle"] for row in routing_rows(bus)
               if row["session"] != bus.session]
    for name in os.listdir(bus.state):
        if name.startswith("handle.") and name != f"handle.{bus.session}":
            handles.append(state.read_file(os.path.join(bus.state, name)))
    for handle in handles:
        if handle and "-" in handle:
            held.add(handle.split("-", 1)[1])
    return held


def _pick_number(bus):
    """A random number no other window holds.

    The caller holds the names lock, so two windows opening at once cannot
    both take the one number that was free.

    Args:
        bus (Bus): Bus from connect().

    Returns:
        str: NAME_DIGITS digits, with one more only once every number of
            that length is taken.
    """
    held = _held_numbers(bus)
    for digits in itertools.count(constants.NAME_DIGITS):
        free = [str(number)
                for number in range(10 ** (digits - 1), 10 ** digits)
                if str(number) not in held]
        if free:
            return random.choice(free)
    raise AssertionError("unreachable: the digits never run out")


def ensure_handle(bus, agent):
    """Give this window its own name, once, the first time it registers.

    Args:
        bus (Bus): Bus from connect().
        agent (str): CLI name, which leads the generated name.

    Returns:
        str: The handle this window is published as, for life.
    """
    existing = state.read_handle(bus.state, bus.session)
    if existing:
        return existing
    with open(os.path.join(bus.state, "names.lock"), "a",
              encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        existing = state.read_handle(bus.state, bus.session)
        if existing:
            return existing
        handle = f"{agent}-{_pick_number(bus)}"
        path = state.handle_path(bus.state, bus.session)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as target:
            target.write(handle)
        os.replace(temporary, path)
    bus.handle = handle
    state.republish(bus, "handle", handle)
    return handle


def names_a_chore(task):
    """Whether a task describes reading the bus instead of any work.

    Args:
        task (str): The task, with or without a CLI prefix or number.

    Returns:
        bool: True when every word in it is bus upkeep.
    """
    words = [word for word in re.split(r"[-_]", normalise_task(task))
             if word]
    return bool(words) and all(word in constants.CHORE_WORDS
                               for word in words)


def normalise_task(task):
    """Reduce what a window typed to the task itself.

    Windows were told for months to name themselves claude-<task>, and
    some will keep typing it that way, or with a number from the old
    scheme. Both are the same task as the bare word.

    Args:
        task (str): The task as typed.

    Returns:
        str: The task without a CLI prefix or trailing number.
    """
    task = constants.NAME_NUMBER.sub("", (task or "").strip().lower())
    for agent in constants.AGENT_NAMES:
        if task.startswith(agent + "-"):
            return task[len(agent) + 1:]
    return task


def set_task(bus, task):
    """Say what this window is working on, beside its name.

    The name identifies the window and never changes; the task describes
    it and changes with the work. Two windows on one task is ordinary --
    a claude and a codex pairing on it is what related-window routing is
    for -- so a task is not unique, and nothing is numbered.

    Args:
        bus (Bus): Bus from connect().
        task (str): The work, e.g. "sso-login". A leading "claude-" or a
            trailing "-001" from the old naming scheme is dropped.

    Returns:
        str: The task now published.

    Raises:
        ValueError: The task is malformed, names a CLI, or only
            describes checking the bus.
    """
    task = normalise_task(task)
    state.check_name(task)
    if task in constants.AGENT_NAMES:
        raise ValueError(f"{task!r} is a CLI, not a task")
    if names_a_chore(task):
        raise ValueError(
            f"{task!r} describes checking the bus, which every window "
            "does -- leave the task unset until you are given one, then "
            "name that")
    path = state.task_path(bus.state, bus.session)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as target:
        target.write(task)
    os.replace(temporary, path)
    state.republish(bus, "task", task)
    return task


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
        # A row can be inside the heartbeat window and still belong to a
        # process that has since exited. Counting it as an addressee
        # would hold the message open for a reader that can never read,
        # so it could only ever leave the bus by expiring.
        if identity.session_dead(session, record):
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
        row["task"] = state.task_of(bus.state, row["session"], row["agent"],
                                    row["handle"])
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
    record["task"] = state.task_of(bus.state, bus.session, agent,
                                   record["handle"])
    if bus.parent_session:
        record["parent"] = bus.parent_session
        parent_handle = state.read_handle(bus.state, bus.parent_session)
        if parent_handle:
            record["display_handle"] = (
                f'{record["handle"]} (helper of {parent_handle})'
            )
    # The CLI process this row belongs to, when it can be found, so the
    # row can later be shown to be dead rather than merely quiet. Only
    # written when known: a hook can see the window above it, while a
    # server started by a shared daemon cannot, and overwriting a good
    # answer with None would lose the one chance to record it.
    window = identity.session_pid()
    if window:
        record["window"] = window
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
    ensure_handle(bus, agent)
    if agent == "codex":
        parent_session = identity.get_parent_session(bus)
        if parent_session:
            bus.parent_session = parent_session
    status = info.pop("status", "idle")
    defaults = {"pid": os.getpid(), "cwd": os.getcwd()}
    defaults.update(info)
    return touch(bus, agent, status=status, **defaults)


def _reap_orphans(bus, alive):
    """Delete per-session state belonging to no surviving presence file.

    job, handle and task are keyed by session alone, so they outlive the
    presence row that named the agent. They are only removed once no
    presence file mentions the session at all, because a window can
    rename itself and leave a second presence file under the old name.

    Args:
        bus (Bus): The bus whose state directory is being swept.
        alive (set[str]): Set of session ids that still have a presence file.
    """
    for name in os.listdir(bus.state):
        parts = name.split(".", 1)
        if parts[0] not in ("job", "handle", "task"):
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
        # Two reasons to delete a row. Being provably dead is checked
        # first and ignores the clock: an hour of a ghost on the roster
        # is an hour of a name somebody might address mail to.
        if (idle >= constants.PRESENCE_REAP_SECONDS
                or identity.session_dead(session, record)):
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
        row["task"] = state.task_of(bus.state, session, agent, row["handle"])
        row["pending"] = 0
        rows.append(row)
    _reap_orphans(bus, alive)
    return rows
