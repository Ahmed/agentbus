#!/usr/bin/env python3
"""Observe unread mail without consuming it; notifications are opt-in.

Hooks deliver mail only when the CLI runs a tool or reaches a turn
boundary. A window at an empty prompt has no active hook, so this watcher
cannot deliver mail into its conversation or start a turn.

The watcher is silent by default. --notify enables desktop notifications
and --bell enables the terminal bell; each must be requested explicitly.
--no-notify overrides --notify for compatibility with older invocations.

Never consume. bus.peek scans without moving the cursor or settling
mail, leaving delivery to the window's hook.

Never touch presence. A watcher must not make an idle window look active.

Started by session_hook.py at SessionStart, one per window, deduplicated
by an exclusive lock on a file named for the session. It exits when the
CLI process it was started for goes away.
"""

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
import time

import bus

# How often to look. The cost of a look is a stat of one file and, only
# when that file has changed, a scan of the tail of it -- so this is
# closer to free than the interval suggests. Kept well under the ten
# minute TTL so enabled notifications can arrive before expiry.
POLL_SECONDS = 2.0

# How long a message must sit unread before it is worth ringing about.
# A window that is mid-task runs a tool every few seconds and its
# PostToolUse hook collects the mail without anyone's help; ringing for
# that is noise about a problem that does not exist. The bell is for mail
# that is genuinely stuck, which is what it still being here after this
# long means.
GRACE_SECONDS = 5.0

# Asks the terminal for a bell. A bell is the only thing worth writing to
# a tty that a TUI owns: it produces no glyph, so it cannot corrupt the
# frame the CLI is in the middle of drawing.
BELL = b"\a"

# Nothing here is allowed to make noise on its own stdout -- it is
# detached from the CLI, and anything it printed would either vanish or
# land in the middle of the window's display.
LOG_ENV = "AGENTBUS_WATCHER_LOG"

# How long to wait for the notifier before giving up on it. Generous for
# something that normally returns in milliseconds, and short enough that
# a broken session bus costs one poll rather than the whole watcher.
NOTIFY_TIMEOUT_SECONDS = 5.0


def _tty_of(pid):
    """The terminal a process is reading from, if it has one.

    Read from the process's own stdin rather than guessed, because the
    watcher is detached by then and has no controlling terminal of its
    own to fall back on.

    Returns:
        A path under /dev, or None for a window with no tty -- which is
        what a CLI launched from an IDE or run headless looks like.
    """
    try:
        target = os.readlink(f"/proc/{pid}/fd/0")
    except (IOError, OSError):
        return None
    if target.startswith("/dev/pts/") or target.startswith("/dev/tty"):
        return target
    return None


def _alive(pid):
    """Is the window we were started for still running?"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _claim(state, agent, session):
    """Take the one-watcher-per-window lock, or report it is taken.

    The lock is held for the life of the process by leaving the
    descriptor open; it is released by exiting, including by being
    killed, which matters because nothing here gets a clean shutdown. A
    session that starts, is resumed, and is cleared fires SessionStart
    three times and must not end up with three watchers ringing in
    unison.

    A raw descriptor rather than a file object on purpose: a file object
    hands the lock back if it is ever garbage collected, and an integer
    cannot be.

    Returns:
        The open descriptor holding the lock, or None if another watcher
        has it.
    """
    path = os.path.join(state, f"watch.{agent}.{session}.lock")
    guard = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        os.close(guard)
        return None
    os.ftruncate(guard, 0)
    os.write(guard, str(os.getpid()).encode("ascii"))
    return guard


def _describe(messages):
    """A one-line summary for the notification body."""
    first = messages[0]
    sender = first.get("from_handle") or first.get("from") or "someone"
    kind = first.get("kind") or "message"
    text = " ".join((first.get("text") or "").split())
    if len(text) > 90:
        text = text[:89] + "…"
    line = f"{kind} from {sender}: {text}"
    if len(messages) > 1:
        line += f"\n(+{len(messages) - 1} more waiting)"
    return line


def _ring(tty):
    """Ask the window's terminal for a bell, best effort."""
    if not tty:
        return
    try:
        with open(tty, "wb", buffering=0) as handle:
            handle.write(BELL)
    except (IOError, OSError):
        pass


def _notify(handle_name, messages, notifier):
    """Raise a desktop notification, best effort.

    Best effort throughout: a machine with no notification daemon, or a
    session with no bus address to reach one, is not a reason for this to
    die and stop ringing bells it can still ring.

    Waited for, under a timeout, rather than left to run: notify-send
    hands the notification to the daemon and returns in milliseconds, and
    a watcher that never reaps it accumulates a zombie per bell for as
    long as the window is open. The timeout is there because a broken
    session bus is the one case where it would not return at all, and a
    stuck notifier must not cost the bell it was raised for.
    """
    if not notifier:
        return
    title = f"agentbus: {len(messages)} for {handle_name}"
    try:
        subprocess.run([notifier, "-a", "agentbus", "-u", "normal",
                        title, _describe(messages)],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=NOTIFY_TIMEOUT_SECONDS,
                       check=False)
    except (IOError, OSError, subprocess.TimeoutExpired):
        pass


def _log(message):
    """Write a line somewhere findable, when asked to.

    Off unless AGENTBUS_WATCHER_LOG names a file. A detached process with
    nowhere to print is hard to debug, and the alternative -- printing to
    the inherited descriptors -- is what corrupts a TUI.
    """
    path = os.environ.get(LOG_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.0f} {os.getpid()} {message}\n")
    except (IOError, OSError):
        pass


def _ripe(waiting, first_seen, announced, now):
    """The mail that has sat unread long enough to be worth a bell.

    Also forgets anything that has left the bus, so the same window can
    be rung again if that sender writes again later. Both dictionaries
    are updated in place.

    Returns:
        The records to ring about, in the order the bus holds them.
    """
    present = set()
    ready = []
    for record in waiting:
        key = record.get("id")
        if not key:
            continue
        present.add(key)
        first_seen.setdefault(key, now)
        if key in announced:
            continue
        if now - first_seen[key] >= GRACE_SECONDS:
            ready.append(record)

    for key in list(first_seen):
        if key not in present:
            first_seen.pop(key, None)
            announced.discard(key)

    return ready


def main():
    """Observe waiting mail until the window goes away, silently by default.

    Returns:
        A process exit status. Always 0: every way this ends -- the
        window closing, another watcher already holding the lock -- is a
        watcher with nothing left to do rather than a failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True,
                        help="the CLI name this window answers to")
    parser.add_argument("--session", required=True,
                        help="the session id, passed in rather than guessed: "
                             "this process is detached, so walking the "
                             "process tree would find init")
    parser.add_argument("--cwd", default="",
                        help="the session's working directory, which is "
                             "where its job is derived from")
    parser.add_argument("--pid", type=int, default=0,
                        help="the CLI process to follow; the watcher exits "
                             "when it does")
    parser.add_argument("--notify", action="store_true",
                        help="enable desktop notifications (off by default)")
    parser.add_argument("--bell", action="store_true",
                        help="enable the terminal bell (off by default)")
    parser.add_argument("--no-notify", action="store_true",
                        help="disable desktop notifications, overriding "
                             "--notify; retained for compatibility")
    options = parser.parse_args()

    client = bus.connect(session=options.session, cwd=options.cwd or None)
    guard = _claim(client.state, options.agent, options.session)
    if guard is None:
        _log(f"another watcher already holds {options.session}")
        return 0

    window = options.pid or 0
    tty = _tty_of(window) if options.bell and window else None
    notifier = (shutil.which("notify-send")
                if options.notify and not options.no_notify else None)
    _log(f"watching {options.agent} session={options.session} tty={tty}")

    # Ids already rung for, and when each was first seen waiting. Kept in
    # memory only: a watcher that restarts has no history, and ringing
    # once more about mail that is genuinely still unread is the harmless
    # direction to be wrong in.
    first_seen = {}
    announced = set()

    while True:
        if window and not _alive(window):
            _log(f"window {window} gone")
            return 0

        try:
            waiting = bus.peek(client, options.agent, options.session)
        except (IOError, OSError, ValueError):
            # A compaction rewriting the file underneath us, or /tmp
            # cleared out from under everything. Neither is worth dying
            # over; the next look will find whatever is there.
            waiting = []

        ripe = _ripe(waiting, first_seen, announced, time.time())

        if ripe:
            handle_name = bus.current_handle(client, options.agent)
            if options.bell:
                _ring(tty)
            if notifier:
                _notify(handle_name, ripe, notifier)
            announced.update(record["id"] for record in ripe)
            _log("observed unread " + ",".join(r["id"] for r in ripe))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
