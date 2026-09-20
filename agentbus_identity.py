"""Associate CLI processes with conversation inboxes without merging."""

import agentbus_context as context
import fcntl
import json
import os
import re

import agentbus_constants as constants
import agentbus_state as state_store
import agentbus_storage as storage


def _process_name(pid):
    """Read a process's command name, or None if it has gone.

    Args:
        pid (int): Process id whose window identity is inspected.

    Returns:
        str or None: Process command name.
    """
    try:
        with open(f'/proc/{pid:d}/comm', encoding="utf-8") as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return None


def _parent_of(pid):
    """Read a process's parent pid, or None if it has gone.

    Args:
        pid (int): Process id whose window identity is inspected.

    Returns:
        int or None: Parent process id.
    """
    try:
        with open(f'/proc/{pid:d}/stat', encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except (IOError, OSError, IndexError):
        return None
    return int(fields[1])


def _is_shared_daemon(pid):
    """Is this a process every window of a CLI has in common?

    Codex's `app-server` daemon is the parent of MCP servers across all
    Codex windows. Treating it as a session identity collapses them into
    one reader.

    Args:
        pid (int): Process id whose window identity is inspected.

    Returns:
        bool: Whether this process is shared by many windows.
    """
    try:
        with open(f'/proc/{pid:d}/cmdline', "rb") as handle:
            cmdline = handle.read().decode(errors="replace")
    except (IOError, OSError):
        return False
    return "app-server" in cmdline or "--managed-daemon" in cmdline


def session_pid():
    """The pid of the CLI window this process is running under.

    The session id a CLI hands its hooks is a uuid, which identifies the
    conversation and says nothing about the terminal it is drawn on. A
    watcher needs the process itself for both of its jobs: the tty to
    ring is found through it, and its death is how the watcher knows to
    exit rather than outliving the window it was started for.

    Returns:
        The pid, or None when no CLI is among our ancestors -- which is
        the normal answer when bus.py is run from a plain shell.
    """
    pid = os.getpid()
    for _step in range(12):
        parent = _parent_of(pid)
        if not parent or parent == pid:
            return None
        if (_process_name(parent) in constants.CLI_NAMES
                and not _is_shared_daemon(parent)):
            return parent
        pid = parent
    return None


def _process_start(pid):
    """The kernel's start-time token, so a reused pid is not an old window.

    Args:
        pid (int): Process id whose window identity is inspected.

    Returns:
        str or None: Kernel token identifying this process lifetime.
    """
    try:
        with open(f'/proc/{pid:d}/stat', encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[19]
    except (IOError, OSError, IndexError):
        return None


def bound_session(state, key, pid):
    """Look up the conversation id its hook associated with a CLI process.

    Args:
        state (str): Directory containing per-window state files.
        key (str): Stored identity or record key.
        pid (int): Process id whose window identity is inspected.

    Returns:
        str or None: Associated conversation identity.
    """
    try:
        file_path = os.path.join(state, f'session.{key!s}')
        with open(file_path, encoding="utf-8") as handle:
            record = json.load(handle)
        if record.get("start") == _process_start(pid):
            return record.get("session")
    except (IOError, OSError, ValueError):
        pass
    return None


@context.locked
def bind_session(client, agent):
    """Let a dedicated CLI's tools use the session id supplied to its hook.

    Claude supplies a conversation UUID to hooks, but its shell tools and
    MCP server only know the CLI pid. Bind those identities before reading
    mail, preserving any name, job and read position already set by tools.
    A shared Codex daemon must never be bound to one of its many threads.

    Args:
        client (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.
    """
    pid = session_pid()
    if not pid:
        return
    key = f'{_process_name(pid)!s}{pid:d}'
    if key == client.session:
        return
    start = _process_start(pid)
    if start is None:
        return
    path = os.path.join(client.state, f'session.{key!s}')
    with open(path + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if bound_session(client.state, key, pid) == client.session:
            return
        _migrate_identity(client, key)
        _migrate_cursor(client, agent, key)
        temporary = f'{path!s}.{os.getpid():d}.tmp'
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"session": client.session, "start": start}, handle)
        os.replace(temporary, path)
        # Keep pins valid after process exit and distinguish reused pids.
        history = f"{path}.{start}.json"
        with open(history + ".tmp", "w", encoding="utf-8") as handle:
            json.dump({"session": client.session, "start": start}, handle)
        os.replace(history + ".tmp", history)
        # The hook registers the canonical row immediately afterwards.
        state_store.discard(client, f'presence.{agent!s}.{key!s}')
        state_store.discard(client, f'cursor.{agent!s}.{key!s}')
        client.handle = state_store.read_handle(client.state, client.session)
        client.job = (os.environ.get("AGENTBUS_JOB") or state_store.read_job(
            client.state, client.session, client.cwd) or client.job)


def _migrate_identity(client, key):
    """Carry names and jobs into the hook's conversation identity.

    Args:
        client (Bus): Connection for the canonical conversation.
        key (str): Legacy process identity being merged.
    """
    for prefix in ("handle", "job"):
        old = os.path.join(client.state, f"{prefix}.{key}")
        new = os.path.join(client.state, f"{prefix}.{client.session}")
        if os.path.exists(old) and not os.path.exists(new):
            os.replace(old, new)


def _migrate_cursor(client, agent, key):
    """Keep unread mail from either inbox without replaying broadcasts.

    Args:
        client (Bus): Connection for the canonical conversation.
        agent (str): CLI owning the read cursor.
        key (str): Legacy process identity being merged.
    """
    old = os.path.join(client.state, f"cursor.{agent}.{key}")
    if not os.path.exists(old):
        return
    new = storage.cursor_path(client, agent)
    offset = storage.read_cursor(old)
    if os.path.exists(new):
        offset = min(offset, storage.read_cursor(new))
    _migrate_readers(client, key)
    storage.write_cursor_path(new, offset)


def _migrate_readers(client, key):
    """Preserve already-read messages when two inbox identities are joined.

    Args:
        client (Bus): Connection for the canonical conversation.
        key (str): Legacy reader identity in the delivery ledger.
    """
    path = storage.delivery_path(client)
    with open(path + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        ledger = storage.load_delivery(client)
        for entry in ledger.values():
            readers = entry.get("readers", [])
            if key in readers and client.session not in readers:
                readers.append(client.session)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle)
        os.replace(temporary, path)


def session_key(state=None):
    """Identify the CLI window this process belongs to.

    Prefer an explicit environment identity. Codex exports CODEX_THREAD_ID
    to shell tools even when their parent is a shared app-server; a new
    process id on every command would lose names and read positions.

    Otherwise walk up to a dedicated CLI and use the conversation id its
    hook bound to that process. Before the first hook, use the CLI pid.

    This is a fallback, and under Codex it is not good enough: Codex runs
    MCP servers under one shared `codex app-server` daemon, so every
    window resolves to the same pid and they steal each other's mail --
    the exact failure the file was meant to end. Anything holding a real
    session id from its CLI should pass it to connect() instead.

    Args:
        state (str): Directory containing per-window state files.

    Returns:
        str: Conversation id or process fallback.
    """
    supplied = os.environ.get(
        "AGENTBUS_SESSION") or os.environ.get("CODEX_THREAD_ID")
    if supplied:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", supplied):
            raise ValueError("invalid bus session id")
        return supplied
    state = state or constants.STATE_DIR
    pid = os.getpid()
    for _step in range(12):
        parent = _parent_of(pid)
        if not parent or parent == pid:
            break
        name = _process_name(parent)
        if name in constants.CLI_NAMES:
            if not _is_shared_daemon(parent):
                key = f'{name!s}{parent:d}'
                return bound_session(state, key, parent) or key
            # Spawned by the daemon every window shares. Falling through
            # to os.getppid() would hand back that same daemon, which is
            # how the shared-cursor bug survived its first fix. Our own
            # pid is the only per-window identity available, and it is
            # stable because one such server lives per window.
            return f'pid{os.getpid():d}'
        pid = parent
    return f'pid{os.getppid():d}'


def canonical_session(bus, session, generation=None):
    """Follow a process identity without confusing later reuse of its pid.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        session (str or None): Conversation identity; defaults to this window.
        generation (str or None): Kernel lifetime token pinned to the process
            identity.

    Returns:
        str: Current identity for an earlier window session.
    """
    match = re.fullmatch(r"(claude|codex|gemini|node)([0-9]+)", session or "")
    if match and generation is not None:
        path = os.path.join(bus.state, (
            f'session.{session!s}.{generation!s}.json'
        ))
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
            if record.get("start") == generation:
                return record.get("session") or session
        except (IOError, OSError, ValueError):
            pass
        # Older bindings may predate the generation-specific history file.
        try:
            with open(os.path.join(bus.state, f'session.{session!s}'), encoding="utf-8") as handle:
                record = json.load(handle)
            if record.get("start") == generation:
                return record.get("session") or session
        except (IOError, OSError, ValueError):
            pass
        if _process_start(int(match.group(2))) == generation:
            return session
        # Preserve an unreachable identity rather than address a later
        # process that happens to reuse this numeric pid.
        return f'{session!s}@{generation!s}'
    if match:
        return bound_session(
            bus.state, session, int(
                match.group(2))) or session
    return session


def session_generation(session):
    """Pin a process lifetime so reused pids cannot inherit old mail.

    Args:
        session (str or None): Conversation identity; defaults to this window.

    Returns:
        str or None: Kernel token for a process-based identity.
    """
    match = re.fullmatch(r"(claude|codex|gemini|node)([0-9]+)", session or "")
    return _process_start(int(match.group(2))) if match else None
