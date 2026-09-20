#!/usr/bin/env python3
"""Rings the terminal of a window that has mail and is not looking.

The hooks deliver mail only at moments the CLI chooses to fire one: a
tool call, the start of a turn, the end of a turn. A window sitting at an
empty prompt fires none of them, so its mail waits for the operator to
type something -- and the operator has no way of knowing there is
anything to type for. This process closes that last gap, and closes it
the only way that is actually available.

What it does NOT do, because nothing on this machine can: put the message
into the conversation. There is no supported channel from outside a CLI
into a running turn. Keystroke injection through the tty is off
(`dev.tty.legacy_tiocsti = 0` on this kernel and most others since 6.2),
writing to the window's stdout paints characters over a TUI that is busy
redrawing, and the hook interface is a reply to something the CLI asked,
not a door to knock on. `tmux send-keys` is the one real exception and it
requires every CLI to be launched inside tmux, which they are not.

So this rings a bell and raises a desktop notification, and the operator
presses Enter. That is a smaller promise than push delivery and it is an
honest one.

Two rules it must not break:

Never consume. It looks with bus.peek, which scans without moving the
cursor and without settling delivery, so the mail it rings about is still
there for the window's own hook to deliver. A watcher that read the
message would be a watcher that stole it.

Never touch presence. Calling bus.touch here would refresh the
heartbeat of a window that is doing nothing, so the roster would show
every watched window as permanently online and `_live_addressees` would
count it as an addressee that can never read. Presence must keep meaning
"a hook fired recently".

Started by session_hook.py at SessionStart, one per window, deduplicated
by an exclusive lock on a file named for the session. It exits when the
CLI process it was started for goes away, so closing a window takes its
watcher with it.
"""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bus


# How often to look. The cost of a look is a stat of one file and, only
# when that file has changed, a scan of the tail of it -- so this is
# closer to free than the interval suggests. Kept well under the ten
# minute TTL so nothing expires unannounced.
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
        target = os.readlink("/proc/%d/fd/0" % pid)
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

    The lock is held for the life of the process by leaving the file
    open; it is released by exiting, including by being killed, which
    matters because nothing here gets a clean shutdown. A session that
    starts, is resumed, and is cleared fires SessionStart three times and
    must not end up with three watchers ringing in unison.

    Returns:
        The open file keeping the lock, or None if another watcher holds
        it. The caller must keep the returned object alive.
    """
    path = os.path.join(state, "watch.%s.%s.lock" % (agent, session))
    guard = open(path, "a")
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        guard.close()
        return None
    guard.seek(0)
    guard.truncate()
    guard.write(str(os.getpid()))
    guard.flush()
    return guard


def _describe(messages):
    """A one-line summary for the notification body."""
    first = messages[0]
    sender = first.get("from_handle") or first.get("from") or "someone"
    kind = first.get("kind") or "message"
    text = " ".join((first.get("text") or "").split())
    if len(text) > 90:
        text = text[:89] + "…"
    line = "%s from %s: %s" % (kind, sender, text)
    if len(messages) > 1:
        line += "\n(+%d more waiting)" % (len(messages) - 1)
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
    """
    if not notifier:
        return
    title = "agentbus: %d for %s" % (len(messages), handle_name)
    try:
        subprocess.Popen([notifier, "-a", "agentbus", "-u", "normal",
                          title, _describe(messages)],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except (IOError, OSError):
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
        with open(path, "a") as handle:
            handle.write("%.0f %d %s\n" % (time.time(), os.getpid(), message))
    except (IOError, OSError):
        pass


def main():
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
                             "when it does, and rings its tty")
    parser.add_argument("--no-notify", action="store_true",
                        help="ring the tty but raise no desktop "
                             "notification. Exists because testing this "
                             "without it puts real popups on the owner's "
                             "screen, which is exactly what happened.")
    options = parser.parse_args()

    client = bus.connect(session=options.session, cwd=options.cwd or None)
    guard = _claim(client.state, options.agent, options.session)
    if guard is None:
        _log("another watcher already holds %s" % options.session)
        return 0

    window = options.pid or 0
    tty = _tty_of(window) if window else None
    notifier = None if options.no_notify else shutil.which("notify-send")
    _log("watching %s session=%s tty=%s" % (options.agent, options.session,
                                            tty))

    # Ids already rung for, and when each was first seen waiting. Kept in
    # memory only: a watcher that restarts has no history, and ringing
    # once more about mail that is genuinely still unread is the harmless
    # direction to be wrong in.
    first_seen = {}
    announced = set()

    while True:
        if window and not _alive(window):
            _log("window %d gone" % window)
            return 0

        try:
            waiting = bus.peek(client, options.agent, options.session)
        except (IOError, OSError, ValueError):
            # A compaction rewriting the file underneath us, or /tmp
            # cleared out from under everything. Neither is worth dying
            # over; the next look will find whatever is there.
            waiting = []

        now = time.time()
        present = set()
        ripe = []
        for record in waiting:
            key = record.get("id")
            if not key:
                continue
            present.add(key)
            first_seen.setdefault(key, now)
            if key in announced:
                continue
            if now - first_seen[key] >= GRACE_SECONDS:
                ripe.append(record)

        # Forget anything that has left the bus, so the window can be
        # rung again if the same sender writes again later.
        for key in list(first_seen):
            if key not in present:
                first_seen.pop(key, None)
                announced.discard(key)

        if ripe:
            handle_name = bus.current_handle(client, options.agent)
            _ring(tty)
            _notify(handle_name, ripe, notifier)
            announced.update(record["id"] for record in ripe)
            _log("rang for %s" % ",".join(r["id"] for r in ripe))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
