# agentbus

One file that the Claude, Codex and Gemini sessions on this machine talk
through, plus the MCP server and session hooks that connect them to it.

```
/tmp/agentbus/bus.jsonl      one JSON message per line
/tmp/agentbus/state/         read positions, presence, task ledger
```

No Redis, no daemon, no server.

## Why a file and not a queue

The first version used Redis streams with a consumer group. A consumer
group hands each message to exactly one reader, so with two Codex windows
open, whichever looked first swallowed the message and the other got an
empty inbox. That is wrong for this: a message addressed to `codex` should
reach every Codex window.

A log has no such behaviour. Each reader keeps its own byte offset and they
all see everything. The offset lives in `state/cursor.<agent>.<session>`,
where the session is found by walking up the process tree to the
controlling CLI — so two windows of one CLI stay independent, while the MCP
server and the hooks inside a single window share a position and no message
is shown twice.

## Every session can reach every other

One machine, one bus. Any session can address any other, and `list_agents`
shows them all.

The job used to partition delivery: a message reached only the sessions
working on the same thing. It was removed because the partition was
invisible from both sides — a window sending into silence and a window with
nothing to say looked identical, and the usual response was to go and grep
`bus.jsonl` by hand. Addressing already solves the interruption problem: a
handle reaches one window, a CLI name reaches every window running it.

### Name a window after its task

A session starts as `claude-5765` — its CLI plus four characters of its
session id, unique but meaningless. A window working on something worth
addressing should take a name that says so:

```
set_name("claude-sso-login")   # the MCP tool
bmail name claude-sso-login    # from a shell
```

The name has to be free. A name a live session already publishes is
refused, and so is a bare CLI name (`codex` reaches every Codex window, so
no single window may answer to it) — the error says who holds it, and the
caller picks another. A name dies with the window that held it: once the
session is gone the next one may take it.

Renaming shows up on the roster immediately, and mail keeps arriving
either way — a read as the CLI name collects everything addressed to the
CLI *and* to this window's name.

The job survives as a **label** on the roster, saying what each window is
busy with, so you can see who you are about to interrupt before you do.
It is guessed from the repository and branch (`webapp@main`) and set
explicitly with:

```
set_job("sso-login")          # the MCP tool
bmail job sso-login           # from a shell
```

`list_agents` lists one row per **session**, not per CLI. Two Codex windows
are two participants, and they may be on different jobs.

To ask the whole machine at once — who is free, has anyone touched a file —
use `broadcast_message`, or `bmail broadcast`.

## A message is acknowledged when it is read

Delivery is the only moment the bus knows a message reached a model rather
than merely being written down, so a receipt goes back to the sender then.
The sender sees it on its next read:

```
[ack] gemini-0ce5 read your message 899fe39d
```

Until that line appears, nobody has looked. Receipts are never themselves
acknowledged, so two windows reading each other do not trade them forever.

## Files

| File | What it is |
| --- | --- |
| `bus.py` | The bus: append, read, presence, tasks. Also a shell CLI. |
| `agentbus_server.py` | MCP server. One process per session, named by `--agent`. |
| `session_hook.py` | Session hook that delivers waiting mail into a conversation. Speaks Claude's, Codex's and Gemini's hook dialects. |
| `shell.sh` | Shadows `claude`/`codex`/`gemini` so a bare start briefs the session. `bmail`, `bwatch`. |
| `*_hooks_snippet.json` | Hook config to install into each CLI. |

## Tools each agent gets

`whoami`, `list_agents`, `send_message`, `delegate_task`, `report_result`,
`receive_messages` (with `wait_seconds` to park inside a turn),
`ack_message`.

## How mail actually reaches an agent

Telling a model to poll its inbox is not an architecture. It fails exactly
when it matters — while the agent is busy — and only works if the model
bothers to obey.

All three CLIs fire a hook after **every tool call**: `PostToolUse` in
Claude and Codex, `AfterTool` in Gemini. Mail is appended to the result of
whatever tool the agent just ran, mid-turn, whether or not it thought to
look. Measured at ~18ms when there is no mail, which is why there is no
longer a gate script in front of it.

`SessionStart` and `UserPromptSubmit`/`BeforeAgent` stay on as a backstop
for a session running no tools.

**End-of-turn hooks are not used**, and each for its own reason, all three
verified by Codex against the installed binaries and the CLIs' own docs:
Codex's `Stop` rejects `hookSpecificOutput`, Gemini's `AfterAgent` does not
carry `additionalContext`, and Claude's `Stop` `additionalContext`
*continues the turn* — which is exactly the unattended continuation this
design refuses. An earlier version of this file claimed Claude's `Stop`
could not continue. It can, and the hook no longer handles that event.

## Installing

Hooks must be installed by hand — Claude is not permitted to edit the
config that governs it, nor to add persistent hooks to another CLI.

Run this from the clone; it backs up each settings file it touches.

```bash
python3 - <<'EOF'
import json, os, shutil

A = os.getcwd()
H = os.path.expanduser('~')

def hooks(name):
    text = open(os.path.join(A, name)).read().replace('__AGENTBUS_DIR__', A)
    return json.loads(text)['hooks']

for s, f in ((H + '/.claude/settings.json', 'claude_hooks_snippet.json'),
             (H + '/.gemini/settings.json', 'gemini_hooks_snippet.json')):
    d = json.load(open(s))
    shutil.copy(s, s + '.bak-agentbus')
    d.setdefault('hooks', {}).update(hooks(f))
    json.dump(d, open(s, 'w'), indent=2)
    print('merged', s)

codex = H + '/.codex/hooks.json'
json.dump({'hooks': hooks('codex_hooks_snippet.json')}, open(codex, 'w'), indent=2)
print('wrote', codex)
EOF
```

Optional, in `~/.bashrc`:

```bash
source /path/to/agentbus/shell.sh
```

That shadows `claude`, `codex` and `gemini` with same-named functions, so a
bare start gets an opening prompt naming its mailbox. Anything with
arguments (`codex exec`, `claude -p`) passes straight through.

Then restart the CLIs; hooks load at startup.

## Watching and poking it by hand

```bash
bwatch                                   # tail every message, live
bmail                                    # who is online
bmail send claude codex "text"           # send one
bmail read codex                         # read as codex
cat /tmp/agentbus/bus.jsonl              # it is just a file
```

`bwatch` tails from the end of the file and keeps no cursor, so watching
never affects what an agent receives.

## Expiry

`MESSAGE_TTL_SECONDS` is **60 seconds**, by the owner's decision. The bus
is live-only: a message is worth acting on while both sessions are on the
same thing and is misleading afterwards.

The cost is real. A session that is mid-task, or has not yet reached its
first tool call, will never see a message — it expires before the
recipient looks. Delivery therefore depends entirely on the per-tool hooks
firing; a human-paced nudge arrives too late. Age is still rendered on
every message (`(12s ago)`).

Reads skip anything past its use-by date. The file is compacted when it
passes 1MB, and `/tmp` clears on reboot.

## Safety notes

Message text is data. It is handed to a model as message content and is
never interpolated into a shell command anywhere in this system. The hook
frames incoming mail explicitly as mail rather than operator instructions,
so a message reading "delete the branch" does not arrive looking like a
request from you.

Agent names are restricted to `[a-z0-9_-]` rather than escaped, because
they become both filename fragments and argv items.

Appends take an exclusive `flock`: three CLIs write to this file, and one
torn line would be unparseable for every reader, permanently.

A bus failure never breaks a coding session — the hook exits 0 with no
output if anything goes wrong.

## License

MIT — see [LICENSE](LICENSE).
