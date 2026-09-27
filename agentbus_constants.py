"""Shared limits keep routing, storage, and presence on one policy."""

import os
import re

# One file under /tmp, by the owner's decision. /tmp is cleared on reboot,
# which suits a channel whose contents are worthless by the next morning.
BUS_DIR = os.environ.get("AGENTBUS_DIR", "/tmp/agentbus")
BUS_FILE = os.path.join(BUS_DIR, "bus.jsonl")
STATE_DIR = os.path.join(BUS_DIR, "state")
TASK_FILE = os.path.join(STATE_DIR, "tasks.json")

# Ten minutes. This is the backstop, not the usual way a message leaves
# the bus: a message is normally removed the moment it has been read by
# everyone it was addressed to (see _settle_delivery). The TTL only
# catches mail nobody ever looked at.
#
# It was sixty seconds when delivery could only ride on a hook the agent
# happened to fire, which made anything older than a minute misleading. A
# turn can now be woken at its end, so the window in which a message is
# still worth acting on is wider, and an unread one is worth keeping for
# more than a minute.
#
# The cost is still real. A window that is idle at its prompt runs no
# hooks, so mail addressed to it waits for the operator either way; ten
# minutes only widens the odds that something fires first. Age is
# rendered on every message so a reader can judge how stale it is.
MESSAGE_TTL_SECONDS = 600

# Compaction rewrites the file without its expired lines. Triggered by size
# rather than on a timer, because there is no daemon to run a timer.
COMPACT_BYTES = 1024 * 1024

# Presence is a file whose mtime is the heartbeat. Agents get killed and
# Ctrl-C'd constantly; anything relying on a clean shutdown to clear
# presence would show ghosts forever.
PRESENCE_TTL_SECONDS = 120

# Nothing clears a presence file on exit, so every window that ever ran
# stays on the list forever. Past this idle time the row is deleted
# outright along with the session's cursor, job and handle. Kept well
# above PRESENCE_TTL_SECONDS because presence only refreshes when a tool
# call fires a hook: a window sitting idle at a prompt is still alive,
# and one that comes back after a reap simply writes its files again.
PRESENCE_REAP_SECONDS = 3600

# "message" is conversation, "task" is a delegation that expects a "result"
# carrying the same task_id back, and "ack" is the receipt the bus posts
# by itself when a message is handed to its recipient.
MESSAGE_KINDS = ("message", "task", "result", "ack",
                 "project_check", "project_status")

# The job recorded on a message meant for everyone. Delivery no longer
# reads the job at all, so this is now only a label saying the message
# was not about one piece of work.
BROADCAST_JOB = "*"

# Agent names become filename fragments and argv items, so they are
# restricted rather than escaped.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The number on the end of a name from before names were generated
# (codex-sso-001). Still recognised, so a window that took one before this
# version keeps its task until it closes.
NAME_NUMBER = re.compile(r"-[0-9]{3}$")

# What a window is called, as opposed to what it is doing. Every window is
# given one of these when it first registers -- claude-otter, codex-heron
# -- and keeps it for life. It is never chosen by the model and never
# shared: no two windows on the bus hold the same word, whatever their
# CLI, so the roster never shows two rows a reader has to tell apart by a
# number. The task a window is on is a separate label beside it.
#
# Short, unlike each other, and unlike anything a task would be called.
NAME_WORDS = (
    "acacia", "agate", "alder", "amber", "aspen", "badger", "baobab",
    "basalt", "beaver", "beryl", "birch", "bison", "bobcat", "brook",
    "camel", "canyon", "cedar", "coyote", "crane", "cypress", "dove",
    "dune", "egret", "elk", "ferret", "finch", "fir", "fjord", "fox",
    "garnet", "gazelle", "gecko", "geyser", "glacier", "granite", "grove",
    "hare", "hawk", "hazel", "hemlock", "heron", "ibex", "ibis", "jaguar",
    "jay", "juniper", "koala", "lagoon", "larch", "lark", "laurel",
    "lemur", "linden", "lynx", "magpie", "maple", "marble", "marsh",
    "marten", "meadow", "meerkat", "mesa", "mica", "mink", "moose",
    "narwhal", "newt", "oak", "ocelot", "okapi", "onyx", "orca", "osprey",
    "otter", "owl", "plover", "poplar", "possum", "prairie", "puffin",
    "puma", "quartz", "raccoon", "reef", "ridge", "robin", "rowan",
    "seal", "sequoia", "shrew", "spruce", "stoat", "stork", "tapir",
    "tern", "toad", "topaz", "tundra", "valley", "vole", "walrus",
    "willow", "wolf", "wombat", "wren", "yak", "yew", "zebra",
)

# Words for reading the bus rather than for any piece of work. A new
# window is told to check its mail before it has been given anything else
# to do, and left to itself calls its task that -- so every fresh window
# was on "inbox" and the roster could not tell them apart. A task made
# only of these says nothing, and is refused.
CHORE_WORDS = frozenset(("inbox", "mail", "mailbox", "message", "messages",
                         "bus", "agentbus", "check", "read", "wait"))

# The CLIs a hook or MCP server can be running underneath. Finding one of
# these among our ancestors is what identifies the window we belong to.
CLI_NAMES = ("claude", "codex", "gemini", "node")
AGENT_NAMES = ("claude", "codex", "gemini")
