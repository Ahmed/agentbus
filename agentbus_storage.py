"""Persist messages and read positions without torn writes or lost state."""

import fcntl
import json
import os
import time
import uuid

import agentbus_constants as constants
import agentbus_notify as notify


def append(bus, record):
    """Append one message under an exclusive lock.

    The lock matters: three CLIs write to this file and a torn line would
    be unparseable for every reader, permanently.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        record (dict): Message or state record to inspect or persist.
    """
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(bus.path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    # Outside the lock on purpose: the doorbell is best effort and must
    # never be holding the file other windows are waiting to write to.
    notify.ring(record)


def compact(bus):
    """Rewrite the file without spent or expired messages, if it is large.

    Cursors are byte offsets, so a rewrite moves every reader's position.
    They are reset to the new end of file rather than rescanning: the
    messages dropped were expired anyway, and the alternative is
    re-delivering an hour of old traffic to everyone at once.

    Args:
        bus (Bus): Connection whose shared bus state is used.
    """
    try:
        if os.path.getsize(bus.path) < constants.COMPACT_BYTES:
            return
    except OSError:
        return

    cutoff = time.time() - constants.MESSAGE_TTL_SECONDS
    consumed = consumed_ids(bus)
    kept = []
    with open(bus.path, encoding="utf-8") as handle:
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
            with open(bus.path + ".tmp", "w", encoding="utf-8") as out:
                out.writelines(kept)
            os.replace(bus.path + ".tmp", bus.path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    end = os.path.getsize(bus.path)
    for name in os.listdir(bus.state):
        if name.startswith("cursor."):
            write_cursor_path(os.path.join(bus.state, name), end)


def delivery_path(bus):
    """Where the record of who has read what is kept.

    Args:
        bus (Bus): Connection whose shared bus state is used.

    Returns:
        str: Shared delivery ledger path.
    """
    return os.path.join(bus.state, "delivered.json")


def load_delivery(bus):
    """Read the ledger, tolerating it not existing yet.

    Args:
        bus (Bus): Connection whose shared bus state is used.

    Returns:
        dict: Delivery records indexed by message id.
    """
    try:
        with open(delivery_path(bus), encoding="utf-8") as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return {}


def consumed_ids(bus):
    """Message ids that have been read by everyone they were sent to.

    Args:
        bus (Bus): Connection whose shared bus state is used.

    Returns:
        set[str]: Message ids settled for all addressees.
    """
    return set(key for key, entry in load_delivery(bus).items()
               if entry.get("done"))


def seen_ids(bus):
    """Mail already read by this session, or finished for every recipient.

    The ledger also prevents redelivery if two formerly separate inbox
    identities are joined and their cursors have to be reconciled.

    Args:
        bus (Bus): Connection whose shared bus state is used.

    Returns:
        set[str]: Message ids already settled or read by this window.
    """
    return {key for key, entry in load_delivery(bus).items()
            if entry.get("done") or bus.session in entry.get("readers", [])}


def cursor_path(bus, agent):
    """Where this reader's position in the file is kept.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        agent (str): CLI name identifying this window on the bus.

    Returns:
        str: This window's saved read-position path.
    """
    return os.path.join(bus.state, f'cursor.{agent!s}.{bus.session!s}')


def read_cursor(path):
    """Return a saved byte offset, or 0 when this reader is new.

    Args:
        path (str): State file path.

    Returns:
        int: Saved byte offset, or zero for a new reader.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip() or 0)
    except (IOError, OSError, ValueError):
        return 0


def write_cursor_path(path, offset):
    """Save a byte offset, replacing the file atomically.

    Args:
        path (str): State file path.
        offset (int): Byte position in the shared message log.
    """
    temporary = f'{path!s}.{os.getpid():d}.tmp'
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(str(offset))
    os.replace(temporary, path)


def scan(bus, offset):
    """Read complete lines from an offset.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        offset (int): Byte position in the shared message log.

    Returns:
        A (entries, end_offset) pair, where each entry is a
        (record, offset_just_past_it) tuple. A trailing partial line,
        which can exist while a writer is mid-append, is left for the
        next read.
    """
    entries = []
    try:
        with open(bus.path, encoding="utf-8") as handle:
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


def _load_tasks(bus):
    """Read the task ledger, tolerating it not existing yet.

    Args:
        bus (Bus): Connection whose shared bus state is used.

    Returns:
        dict: Task records indexed by task id.
    """
    try:
        file_path = os.path.join(bus.state, 'tasks.json')
        with open(file_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return {}


def record_task(bus, task_id, **fields):
    """Store or update the record for a delegated task.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        task_id (str): Identifier tying a delegated task to its result.
        **fields (object): Task record fields to create or update.

    Returns:
        str: Updated task id.
    """
    path = os.path.join(bus.state, "tasks.json")
    with open(path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            tasks = _load_tasks(bus)
            entry = tasks.setdefault(task_id, {})
            entry.update({k: v for k, v in fields.items() if v is not None})
            entry["updated"] = time.time()
            temporary = f'{path!s}.{os.getpid():d}.tmp'
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(tasks, handle)
            os.replace(temporary, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return task_id


def get_task(bus, task_id):
    """Return a task record, or an empty dict if unknown.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        task_id (str): Identifier tying a delegated task to its result.

    Returns:
        dict: Task record, or an empty dictionary when absent.
    """
    return _load_tasks(bus).get(task_id, {})


def new_task_id():
    """Mint an id tying a delegated task to the result that answers it.

    Returns:
        str: New task identifier.
    """
    return f'task_{uuid.uuid4().hex[:10]!s}'
