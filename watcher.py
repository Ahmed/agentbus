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

import agentbus_notify as notify
import bus

# How often to look. The cost of a look is a stat of one file and, only
# when that file has changed, a scan of the tail of it -- so this is
# closer to free than the interval suggests. Kept well under the ten
# minute TTL so enabled notifications can arrive before expiry.
POLL_SECONDS = 2.0

# The doorbell means a look no longer has to be scheduled: a subscriber
# blocks until the instant something is appended. This is the ceiling on
# how long that block lasts, not how often anything is checked -- the
# loop wakes on the ring, or on this, whichever comes first.
#
# Kept short anyway, because expiry runs on a clock and a window ringing
# about mail should notice when that mail goes away.
LISTEN_SECONDS = 20.0

# How long a message must sit unread before it is worth ringing about.
# A window that is mid-task runs a tool every few seconds and its
# PostToolUse hook collects the mail without anyone's help; ringing for
# that is noise about a problem that does not exist. The bell is for mail
# that is genuinely stuck, which is what it still being here after this
# long means.
GRACE_SECONDS = 5.0

# Waking waits far less, because it is not the same act. A bell
# interrupts a person and is worth being sure about; starting a turn
# costs a window a few seconds of its own time, is silent, and queues
# harmlessly behind whatever that window is already doing. Being early
# and occasionally unnecessary is the cheap direction to be wrong in,
# and waiting five seconds to deliver something that arrived in seventy
# milliseconds is the expensive one.
WAKE_GRACE_SECONDS = 0.5

# Asks the terminal for a bell. A bell is the only thing worth writing to
# a tty that a TUI owns: it produces no glyph, so it cannot corrupt the
# frame the CLI is in the middle of drawing.
BELL = b"\a"

# Nothing here is allowed to make noise on its own stdout -- it is
# detached from the CLI, and anything it printed would either vanish or
# land in the middle of the window's display.
LOG_ENV = "AGENTBUS_WATCHER_LOG"

# The CLIs that can be made to start a turn from outside, and the command
# that does it. Codex hosts its sessions in a shared app-server daemon,
# and a thread that daemon holds can be handed a queued message which
# begins a turn -- so for codex alone, mail can be delivered to a window
# nobody is typing into rather than merely rung about.
#
# The agentbus session id is the codex thread id, which is what makes
# this a lookup rather than a search.
#
# Two conditions the daemon imposes, both of which fail quietly here: the
# thread must still be loaded in memory, and it must not be interrupted.
# A window that has exited leaves a queued message pending with no expiry
# of its own, so this is only ever sent for mail already waiting.
WAKE_COMMANDS = {"codex": ("codex", "queue", "--thread")}

# Set AGENTBUS_WAKE=0 to ring without ever starting a turn.
WAKE_ENV = "AGENTBUS_WAKE"

# A wake is a subprocess talking to a daemon over a socket. Bounded so a
# daemon that has stopped answering costs one poll, not the watcher.
WAKE_TIMEOUT_SECONDS = 15.0

# Quoted into the wake message so the window is told exactly how to
# collect its mail, from wherever this copy of the bus lives.
BUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bus.py")

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


def wake(agent, session, messages):
    """Ask this window's CLI to start a turn, where that is possible.

    The bell exists because nothing could do this. Where something can,
    it is strictly better: the window reads its mail and answers without
    the operator being involved at all.

    Says only that mail is waiting. The contents stay on the bus, so a
    queued wake that fires late cannot put a stale message in front of
    the model as though it had just arrived -- and, for a message still
    held, cannot disclose what the window has not agreed to receive.

    Args:
        agent (str): CLI name the window answers to.
        session (str): That window's session, which for codex is also
            its thread id.
        messages (list[dict]): The mail being rung about.

    Returns:
        bool: True when the wake was accepted. False when this CLI has
            no wake, when it is switched off, or when the daemon refused
            -- each of which leaves the bell to do what it always did.
    """
    if os.environ.get(WAKE_ENV) == "0":
        return False
    command = WAKE_COMMANDS.get(agent)
    if not command or not session:
        return False
    if shutil.which(command[0]) is None:
        return False

    text = (f"Agent bus: {len(messages)} message(s) are waiting for this "
            "window. Nobody typed this -- the bus started your turn "
            f"because mail arrived. Read it with: python3 {BUS_PATH} "
            f"read {agent}")
    try:
        finished = subprocess.run([*command, session, "--message", text],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  timeout=WAKE_TIMEOUT_SECONDS,
                                  check=False)
    except (IOError, OSError, subprocess.TimeoutExpired):
        return False
    return finished.returncode == 0


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


def _due(waiting, first_seen, announced):
    """Split the waiting mail by what it has earned.

    Two thresholds over one scan: anything old enough to wake a window,
    and the older subset that has earned interrupting a person.

    Args:
        waiting (list[dict]): The mail currently on the bus for us.
        first_seen (dict): When each id was first observed.
        announced (set): Ids already acted on.

    Returns:
        tuple[list, list]: Records worth waking for, and worth ringing.
    """
    now = time.time()
    ripe = _ripe(waiting, first_seen, announced, now, WAKE_GRACE_SECONDS)
    loud = _ripe(waiting, first_seen, announced, now, GRACE_SECONDS)
    return ripe, loud


def _next_look(first_seen, announced, ceiling):
    """How long we may block before something becomes due.

    A message held back by its grace has no second ring coming. If the
    loop blocks past the moment that message ripens, the grace stops
    being a half-second delay and becomes however long the block was.

    Args:
        first_seen (dict): When each waiting id was first observed.
        announced (set): Ids already acted on, which are not due again.
        ceiling (float): The longest block to allow when nothing is due.

    Returns:
        float: Seconds to block for.
    """
    pending = [seen for key, seen in first_seen.items()
               if key not in announced]
    if not pending:
        return ceiling
    due = min(pending) + WAKE_GRACE_SECONDS - time.time()
    if due <= 0:
        return 0.0
    return min(ceiling, due)


def _ripe(waiting, first_seen, announced, now, grace):
    """The mail that has sat unread long enough to act on.

    Also forgets anything that has left the bus, so the same window can
    be rung again if that sender writes again later. Both dictionaries
    are updated in place.

    Args:
        waiting (list[dict]): The mail currently on the bus for us.
        first_seen (dict): When each id was first observed, updated here.
        announced (set): Ids already acted on.
        now (float): The current time.
        grace (float): How long a message must have waited to count.

    Returns:
        The records to act on, in the order the bus holds them.
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
        if now - first_seen[key] >= grace:
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

        ripe, loud = _due(waiting, first_seen, announced)

        if ripe:
            handle_name = bus.current_handle(client, options.agent)
            # Starting a turn is tried first. A window that wakes and
            # reads does not need to be rung, and ringing it anyway is
            # noise for an operator who was never required.
            woken = wake(options.agent, options.session, ripe)
            if not woken and loud and options.bell:
                _ring(tty)
            if not woken and loud and notifier:
                _notify(handle_name, loud, notifier)
            if woken or loud:
                announced.update(record["id"] for record in ripe)
                _log(("woke for " if woken else "observed unread ")
                     + ",".join(r["id"] for r in ripe))

        # Never out-sleep mail that is only waiting on its grace. The
        # ring for it has already been and gone -- it fired before this
        # loop came back round to subscribe -- so nothing further will
        # arrive to wake us, and blocking for the full ceiling would
        # turn a half-second grace into the whole listen window.
        listen = _next_look(first_seen, announced, LISTEN_SECONDS)

        # Otherwise block on the doorbell rather than sleeping through
        # it, and never come back faster than the old interval when
        # there is no doorbell -- a watcher that spun here would burn a
        # core for as long as Redis stayed down.
        notify.wait_or_sleep(options.agent, options.session,
                             listen, min(listen, POLL_SECONDS))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
