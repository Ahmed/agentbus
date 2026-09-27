"""Keep window names and jobs consistent across short-lived commands."""

import json
import os
import re

import agentbus_constants as constants


def default_handle(agent, session):
    """The name a session publishes if it does not choose one.

    Args:
        agent (str): CLI name identifying this window on the bus.
        session (str or None): Conversation identity; defaults to this window.

    Returns:
        str: Generated window handle.
    """
    return f'{agent!s}-{session[-4:].lower()!s}'


def handle_path(state, session):
    """Where a session's chosen published name is remembered.

    Args:
        state (str): Directory containing per-window state files.
        session (str or None): Conversation identity; defaults to this window.
    """
    return os.path.join(state, f'handle.{session!s}')


def read_handle(state, session):
    """Return the name a session published earlier in its life, if any.

    Args:
        state (str): Directory containing per-window state files.
        session (str or None): Conversation identity; defaults to this window.

    Returns:
        str or None: Previously published window name.
    """
    try:
        with open(handle_path(state, session), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except (IOError, OSError):
        return None


def current_handle(bus, agent):
    """The name this session publishes right now.

    Args:
        bus (Bus): Bus from connect().
        agent (str): The CLI name, used only to build the default.

    Returns:
        The chosen name, or the generated one if none was chosen.
    """
    return (bus.handle or read_handle(bus.state, bus.session)
            or default_handle(agent, bus.session))


def current_task(bus, agent):
    """What this session says it is working on.

    Args:
        bus (Bus): Bus from connect().
        agent (str): The CLI name, for reading a pre-generation name.

    Returns:
        str or None: The task, or None before the window has said.
    """
    return task_of(bus.state, bus.session, agent, current_handle(bus, agent))


def republish(bus, field, value):
    """Write a new name or task into this session's presence rows at once.

    The roster reads both out of the presence file, which is only
    rewritten on the next heartbeat. Without this, a window that has just
    been named, or has just said what it is working on, still shows the
    old value to everyone deciding whom to write to -- including the next
    window checking which names are free.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        field (str): "handle" or "task".
        value (str): The new value.
    """
    tail = "." + bus.session
    for name in os.listdir(bus.state):
        if not name.startswith("presence.") or not name.endswith(tail):
            continue
        path = os.path.join(bus.state, name)
        try:
            with open(path, encoding="utf-8") as source:
                record = json.load(source)
        except (IOError, OSError, ValueError):
            continue
        record[field] = value
        temporary = f'{path!s}.{os.getpid():d}.tmp'
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(record, target)
        os.replace(temporary, path)


def task_path(state, session):
    """Where a session's declared task is remembered.

    Args:
        state (str): Directory containing per-window state files.
        session (str or None): Conversation identity.

    Returns:
        str: Path of the task file.
    """
    return os.path.join(state, f'task.{session!s}')


def task_of(state, session, agent, handle):
    """What a window says it is working on, or None if it has not said.

    A window named before names were generated carried its task inside
    the name -- codex-sso-001 -- and keeps that name until it closes, so
    such a name still counts as saying the task. A generated name never
    ends in a number, so it is never mistaken for one.

    Args:
        state (str): Directory containing per-window state files.
        session (str or None): The window's conversation identity.
        agent (str): The window's CLI name.
        handle (str or None): The window's published name.

    Returns:
        str or None: The task.
    """
    declared = read_file(task_path(state, session))
    if declared:
        return declared
    if not handle or not constants.NAME_NUMBER.search(handle):
        return None
    stem = constants.NAME_NUMBER.sub("", handle)
    if stem.startswith(agent + "-"):
        stem = stem[len(agent) + 1:]
    return stem if stem and stem != agent else None


def job_path(state, session):
    """Where a session's chosen job name is remembered.

    Args:
        state (str): Directory containing per-window state files.
        session (str or None): Conversation identity; defaults to this window.
    """
    return os.path.join(state, f'job.{session!s}')


def _job_cwd_path(state, cwd):
    """Where the job last declared for a directory is remembered.

    An MCP server identifies its session by the CLI's process id, which
    changes whenever the CLI restarts -- orphaning the job that session
    declared and making it refuse to send until someone declares it
    again. A directory is the stabler thing: it is what the job is
    really about, so a new session working there inherits the
    declaration instead of nagging.

    Args:
        state (str): Directory containing per-window state files.
        cwd (str or None): Working directory used to infer the job.

    Returns:
        str: Path of the directory job declaration.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", os.path.abspath(cwd).lower()).strip("_")
    return os.path.join(state, f'jobcwd.{slug or 'root'!s}')


def read_file(path):
    """Read a small state file, or None when it is not there.

    Args:
        path (str): State file path.

    Returns:
        str or None: File contents, or None when unavailable.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except (IOError, OSError):
        return None


def read_job(state, session, cwd=None):
    """Return the job in force: this session's, else this directory's.

    Args:
        state (str): Directory containing per-window state files.
        session (str or None): Conversation identity; defaults to this window.
        cwd (str or None): Working directory used to infer the job.

    Returns:
        str or None: Session or working-directory job declaration.
    """
    declared = read_file(job_path(state, session))
    if declared:
        return declared
    return read_file(_job_cwd_path(state, cwd or os.getcwd()))


def set_job(bus, job):
    """Declare what this session is working on.

    A label for the roster, so another window can see what this one is
    busy with before interrupting it. It does not gate delivery; every
    session on the machine is reachable from every other.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        job (str): Declared label for the work this window is doing.

    Returns:
        str: Effective job label.
    """
    job = (job or "").strip()
    if not job:
        raise ValueError("job name is required")
    for path in (job_path(bus.state, bus.session),
                 _job_cwd_path(bus.state, bus.cwd)):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(job)
    bus.job = job
    return job


def check_name(name):
    """Reject anything unsafe as a filename fragment or argv item.

    Args:
        name (str): Name to validate or look up.

    Returns:
        str: Validated name.
    """
    if not name or not constants.NAME_PATTERN.match(name):
        raise ValueError(
            (
                f'invalid agent name {name!r}: use lowercase letters, '
                f"digits, '-' and '_', max 32 chars"
            ))
    return name


def _git_branch(directory):
    """Read the checked-out branch without shelling out to git.

    Args:
        directory (str or None): Bus directory, or the configured default.

    Returns:
        str or None: Checked-out branch when available.
    """
    head = os.path.join(directory, ".git", "HEAD")
    try:
        with open(head, encoding="utf-8") as handle:
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

    Args:
        directory (str or None): Bus directory, or the configured default.

    Returns:
        str: Repository and branch, or directory label.
    """
    directory = os.path.abspath(directory or os.getcwd())
    walk = directory
    while walk != "/":
        branch = _git_branch(walk)
        if branch:
            return f'{os.path.basename(walk)!s}@{branch!s}'
        walk = os.path.dirname(walk)
    return os.path.basename(directory) or "home"


def discard(bus, name):
    """Delete one state file, tolerating it already being gone.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        name (str): Name to validate or look up.
    """
    try:
        os.unlink(os.path.join(bus.state, name))
    except OSError:
        pass
