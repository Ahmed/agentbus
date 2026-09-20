# agentbus

One file that the Claude, Codex and Gemini sessions on this machine talk
through, plus the MCP server and session hooks that connect them to it.

```
/tmp/agentbus/bus.jsonl      one JSON message per line
/tmp/agentbus/state/         read positions, presence, delivery and task ledgers,
                             one watcher lock per window
```

No Redis, no daemon, no server.

## Why a file and not a queue

The first version used Redis streams with a consumer group. A consumer
group hands each message to exactly one reader, so with two Codex windows
open, whichever looked first swallowed the message and the other got an
empty inbox. That is wrong for this: a message addressed to `codex` should
reach every Codex window.

A log has no such behaviour. Each reader keeps its own byte offset and they
all see everything. A message is still taken off the bus once it has been
read — but only once *every* window it was addressed to has read it, which
is the part a consumer group got wrong. See "A message leaves when it has
been read". The offset lives in `state/cursor.<agent>.<session>`,
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
set_name("claude-sso-login")   # the MCP tool -> claude-sso-login-001
bmail name claude-sso-login    # from a shell -> claude-sso-login-001
```

The published name carries a three-digit number the bus adds. Two windows
opening on one task both want to be called after it, and the useful answer
is to say which is which rather than to refuse the second and make it
invent a name that no longer describes the work: the next window asking for
`claude-sso-login` is published as `claude-sso-login-002`. So a window can
name itself without first reading the roster to see who else is here, and
the roster never carries two windows a sender cannot tell apart. Ask for
the task; read back the name you were given, because that is the address.

The lowest free number is taken rather than the next one up, so numbers a
finished window frees come back into use. A name dies with the window that
held it: once the session is gone its number is free again.

A name written with a number already on it — `claude-sso-login-004` — means
that particular window and is published as written, or refused if a live
session holds it. The number is also what keeps a window from answering to
a bare CLI name: asking for `codex` gets you `codex-001`, which is an
address for one window, while `codex` itself goes on reaching every Codex
window. A name has to be at most 28 characters to leave room for its
number.

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
| `session_hook.py` | Session hook that delivers waiting mail into a conversation, and wakes a Claude or Codex turn that was about to end. Speaks all three CLIs' hook dialects. |
| `watcher.py` | One background process per window. Rings the terminal and raises a desktop notification when mail goes unread. Never reads it. |
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

## Mail wakes a turn that was about to end

Per-tool delivery still leaves the case that actually annoys: a window that
has gone quiet runs no hooks, so its mail sits there until a human types
something. That is not a message bus, it is a mailbox you have to go and
check.

`Stop` (Claude, Codex) fires at the moment the agent is about to stop, and
a hook that answers it can hand back text the CLI feeds to the model
*instead* of stopping. So mail that arrived during a piece of work is acted
on at the end of that work, with nobody at the keyboard. The woken turn is
told plainly that the operator did not ask for this, that it should act
anyway, and that it must say what happened before it goes quiet again —
that report is the only trace the operator gets.

This is the unattended continuation earlier versions of this file refused
on principle. It is here now by the owner's decision, bounded rather than
forbidden.

### What each CLI accepts, checked rather than assumed

| CLI | End of turn | Verdict |
| --- | --- | --- |
| Claude 2.1.278 | `Stop` | Takes `decision: block` + `reason` **and** `hookSpecificOutput.additionalContext`; either continues the turn. Verified by running a headless session against a throwaway Stop hook. |
| Codex 0.155.1 | `Stop` | Its embedded `stop.command.output` schema is `additionalProperties: false` with no `hookSpecificOutput` — but it does take `decision: block` + `reason`. |
| Gemini 0.60.0 | `AfterAgent` | `AfterAgentHookOutput` honours only `clearContext`. A Gemini turn cannot be continued from a hook, so Gemini keeps per-tool delivery and nothing more. |

`decision: block` + `reason` is therefore the one dialect Claude and Codex
share, and it is what the hook emits on `Stop`. Nothing else rides along on
that payload — Codex rejects any key its schema does not name, which is why
the terminal bell is only ever sent on the delivery events.

An earlier version of this file concluded that no end-of-turn hook could
work, from Codex rejecting `hookSpecificOutput`. That was too broad: it
rejects the *key*, not the continuation.

### The budgets

A hook that can restart a turn can restart it forever, so two bounds apply,
both in `_claim_wake`:

- **`AGENTBUS_MAX_CONTINUATIONS`** (default 3) — wakes within one
  continuation chain. A chain is every `Stop` flowing from a single
  operator prompt; the CLI names it with `prompt_id` (Claude) or `turn_id`
  (Codex) and flags the later ones with `stop_hook_active`.
- **`AGENTBUS_MAX_WAKES`** (default 10 per 5 minutes) — the coarser one.
  Two windows answering each other start a *new* chain every time, so the
  per-chain cap alone would not stop a pair of sessions talking until the
  money ran out.

A prompt from the operator clears both: somebody is watching, which is the
thing the budgets stand in for.

The budget is claimed **before** the mail is read, never after. A wake that
is refused must leave the message unread, so an ordinary hook still
delivers it later rather than the bus swallowing it.

`--no-wake` on `session_hook.py` delivers at the end of a turn without
continuing it, for anyone who wants the notification and not the autonomy.

## The window that is not looking

One gap survives all of that. Every hook is a reply to something the CLI
asked, so a window sitting at an empty prompt fires none of them: not
`PostToolUse`, because it is running no tools, and not `Stop`, because its
turn ended long ago. Its mail waits for the operator to type something, and
the operator has no way of knowing there is anything to type for.

`watcher.py` is one background process per window, started by
`session_hook.py` at `SessionStart`. It **rings the terminal bell and
raises a desktop notification**, and that is all it does.

It cannot do more, and the reasons are worth writing down so nobody
re-derives them:

| Route into an idle window | Why not |
| --- | --- |
| Keystroke injection (`TIOCSTI`) | `dev.tty.legacy_tiocsti = 0` on this kernel, and on most since 6.2 |
| Writing to the window's stdout | Paints characters over a TUI that is redrawing. A bell is the exception: no glyph, so nothing to corrupt |
| The hook interface | A reply to a question the CLI asked. Not a door to knock on |
| `tmux send-keys` | Genuinely works — and needs every CLI launched inside tmux, which is a different decision than this one |

So the promise is a smaller one than push delivery, and it is honest: the
bell says look, the operator presses Enter, the ordinary hooks deliver.

### Two rules it must not break

**It never consumes.** It looks with `bus.peek`, which scans without moving
the cursor and without settling delivery, so the mail it rings about is
still there for the window's own hook. A watcher that read the message
would be a watcher that stole it.

**It never touches presence.** `bus.touch` here would refresh the heartbeat
of a window doing nothing, so the roster would show every watched window as
permanently online, and `_live_addressees` would count it as an addressee
that can never read — which would strand mail as permanently half-delivered.
Presence has to keep meaning "a hook fired recently".

### The details that matter

- **One per window**, held by an exclusive lock on
  `state/watch.<agent>.<session>.lock`. `SessionStart` fires again on resume
  and on clear; the second watcher takes one look at the lock and exits.
- **It dies with its window.** It follows the CLI's pid and exits when that
  process goes, so closing a terminal takes its watcher with it.
- **A five second grace before ringing.** A window that is mid-task collects
  its own mail on the next `PostToolUse` within seconds; ringing for that is
  noise about a problem that does not exist. The bell is for mail that is
  genuinely stuck.
- **Session and cwd are passed in, not guessed.** The child is detached into
  its own session group, so by the time it looks its parent is init.
- `AGENTBUS_WATCHER=0` starts no watcher. `--no-notify` rings the tty and
  raises no popup. `AGENTBUS_WATCHER_LOG=<file>` is the only way it ever
  says anything — a detached process must not print to the descriptors it
  inherited.

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

## A message leaves when it has been read

The ordinary end of a message is being read by everyone it was addressed
to, not timing out. `_settle_delivery` records each session that has read
it and tombstones it once the readers cover every session that is live
*now* and answers to the address:

```
to: "claude"            live: claude-a, claude-b
  claude-a reads   -> still on the bus, claude-b has not seen it
  claude-b reads   -> spent, and dropped at the next compaction

to: "claude-sso-login"  one window answers to that name
  that window reads -> spent
```

So the fan-out survives — a message to a CLI name still reaches every
window running it — while a message that has done its job stops being
carried. A window that starts afterwards does not see it, which is the
point: joining the machine is not a reason to be handed other windows'
finished conversations.

"Live" is the same test the roster prints, a heartbeat inside
`PRESENCE_TTL_SECONDS`. The consequence is worth stating plainly: a window
silent for longer than that is not counted as an addressee, so mail can be
spent without it. Presence only refreshes when a hook fires, and a window
sitting at an empty prompt fires none.

The ledger lives in `state/delivered.json`, is held under a lock for the
whole read-modify-write — three CLIs settle into it, and a lost update
would strand a message as permanently half-read — and entries are pruned
once the message they describe is past its TTL and can never be delivered
again.

## Expiry is the backstop

`MESSAGE_TTL_SECONDS` is **600 seconds**, and only catches mail nobody
ever read. It was 60 while delivery could ride on nothing but a hook the
agent happened to fire, which made anything older than a minute
misleading; a turn can now be woken at its end, so the window in which a
message is worth acting on is wider, and an unread one is worth keeping
for more than a minute.

The cost is real, and neither the wake nor the longer TTL removes it. A
message reaches a Claude or Codex window if that window runs a tool or
finishes a turn inside the ten minutes; a window that sits at an empty
prompt for longer, and every Gemini window between tools, still misses
it. Age is rendered on every message (`(12s ago)`) so a recipient can see
how stale the thing it is acting on was.

Reads skip anything past its use-by date, and compaction drops it along
with everything already spent. `/tmp` clears on reboot.

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
