# agentbus

One file that the Claude, Codex and Gemini sessions on this machine talk
through, plus the MCP server and session hooks that connect them to it.

```
/tmp/agentbus/bus.jsonl      one JSON message per line
/tmp/agentbus/state/         read positions, presence, delivery and task ledgers,
                             pending project confirmations, one watcher lock per window
```

No Redis, no daemon, no server.

## Why a file and not a queue

The first version used Redis streams with a consumer group. A consumer
group hands each message to exactly one reader, so with two Codex windows
open, whichever looked first swallowed the message and the other got an
empty inbox. Delivery should follow the intended recipient, not whichever
window happens to read first. An explicit broadcast should reach all its
recipients.

A log lets each reader keep its own byte offset and receive the messages
addressed to it. A message is still taken off the bus once it has been
read — but only once *every* window it was addressed to has read it, which
is the part a consumer group got wrong. See "A message leaves when it has
been read". The offset lives in `state/cursor.<agent>.<session>`,
where the session is the conversation id supplied to hooks. Codex shell
commands use `CODEX_THREAD_ID`; a dedicated Claude process is associated
with its hook's conversation id in `state/session.<cli-pid>`. The binding
also records the process start time so a reused pid cannot inherit another
window's inbox. Existing process-based names, jobs and read positions are
preserved when the hook first establishes that association.

`AGENTBUS_SESSION` can explicitly supply the same identity to an integration
that does not inherit it. In particular, an MCP server launched under a
shared Codex daemon without a thread id still has a separate process-based
inbox; it needs an explicit session identity to share the hooks' inbox. Use
the shell commands inside that Codex window for names and mail until that
integration supplies the id. Running MCP servers must restart to load code
changes; shell commands and hooks load the updated code on their next call.

## Every session can reach every other

One machine, one bus. Any session can address any other, and `list_agents`
shows them all.

An exact handle addresses one window, even across jobs. Sending to a bare
CLI name such as `claude` looks for one related window using the sender's
task name and job. Jobs help choose that recipient; they do not partition
the bus. Before sharing content, the bus asks the chosen window to confirm
it is working on the indicated project. Broadcasting is an explicit action
and uses the same confirmation for each recipient.

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
address for one window, while `codex` itself asks the bus to find a related
Codex window. A name has to be at most 28 characters to leave room for its
number.

Renaming shows up on the roster immediately. Resolved sends are pinned to
the chosen session, so renaming it or reusing its old handle cannot redirect
queued mail. Changing either side's task or job requires a new project
confirmation before further content is shared. Reading as the CLI name
still collects this window's mail and any broadcasts addressed to it.

Use the published handle on the **receiving** side of `send` when the
message belongs to one window:

```
bmail send codex claude-data-export-001 "Update for that Claude window"
bmail send claude codex-data-export-001 "Reply for that Codex window"
```

For ordinary communication, a bare CLI name finds the related window:

```
bmail name codex-data-export                 # -> codex-data-export-001
bmail send codex claude "Update on data-export"    # -> claude-data-export-001
```

The first argument to `send` identifies the sender. The bus resolves the
receiving CLI name in this order:

1. For a reply with `reply_to`, use the original sender when it belongs to
   the receiving CLI.
2. Match the task part of the window name exactly: `codex-data-export-001`
   matches `claude-data-export-002`. The CLI prefix and final three-digit number
   are ignored. If several windows share that task, an exact job match must
   identify one of them.
3. If no task matches, use a unique window with the same job.

Idle windows listed on the roster remain eligible. If no related window
exists or several match, the send fails with candidate handles and appends
nothing. Choose an exact handle from that list or align the windows' task
names and jobs. Shell and MCP responses show the resolved handle and
whether content is queued or waiting for project confirmation, along with
the relevant message or confirmation request id.

If an exact handle is absent, only the project-check question queues for
that handle. The actual content stays off the bus until a window registers
that handle and confirms before the check expires; delivery is then pinned
to that confirming session.

A Codex name set before the thread-identity fix may have belonged to a
short-lived shell process and disappeared. Run `bmail name codex-<task>`
again in the intended Codex window and read back the returned handle.

The job is a **routing hint and roster label** describing the work. It is
guessed from the repository and branch (`webapp@main`) and set explicitly
with:

```
set_job("sso-login")          # the MCP tool
bmail job sso-login           # from a shell
```

`list_agents` lists one row per **session**, not per CLI. Two Codex windows
are two participants, and they may be on different jobs.

To address several windows, broadcast explicitly:

```
bmail broadcast codex claude "Question for all Claude windows"
bmail broadcast codex '*' "Question for all agents"
```

The MCP `broadcast_message` tool addresses all agents. Broadcasts cross
jobs; ordinary sends to a bare CLI select only one related window. Each
broadcast recipient must confirm the project before receiving content.
The broadcast selects the currently registered windows, including idle
ones; windows that join later do not receive it automatically.

### Confirm the project before sharing content

Selecting a window is the first step. If the two sessions have not yet
confirmed their relationship for their current projects, the bus holds the
actual message or task in `state/pending.<id>.json`. Only a `project_check`
question reaches the recipient through the bus, asking whether it is
working on the indicated project. The details are not in that question or
in `bus.jsonl`.

The recipient checks its own current work and explicitly answers using the
confirmation id supplied in the question:

```
bmail confirm claude <confirmation-id> yes
bmail confirm claude <confirmation-id> no
```

With MCP, use `confirm_project(confirmation_id, accept)` with `accept` set
to `true` or `false`. Confirm **yes only if this window is actually working
on the indicated project**. Reading the question or sending an ordinary
reply does not confirm it.

A yes releases the held content and confirms a relationship in both
directions. The two sessions can then exchange messages and tasks without
repeated questions while their task names and jobs remain the same. The
confirmation belongs to the actual sessions and each side's current task
and job, so a replacement session or a project change needs a new check.
An exact recipient handle does not bypass confirmation.

A no prevents delivery of the held content. Without an answer, the details remain
undelivered and the pending request expires after ten minutes. Broadcasts
check each recipient separately, so one window's yes does not release
content to another. Receipt acknowledgments concern delivery only; they do
not confirm a project.

This check follows the existing hook delivery rules. It cannot start a
turn in a fully idle window; its question waits until that window runs a
tool, finishes an active turn, or reads its inbox.

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
| `bus.py` | Public bus API and shell CLI. |
| `agentbus_notify.py` | The Redis doorbell: rings a window when mail lands, and blocks a listener until one arrives. Carries no contents. |
| `tests/` | The test suite, plus the fixtures it shares. Discovered from the repository root. |
| `agentbus_*.py` | Bus implementation modules and shared test helpers. |
| `project_confirmation.py` | Holds content until project confirmation and remembers confirmed session pairs. |
| `agentbus_server.py` | MCP server. One process per session, named by `--agent`. |
| `session_hook.py` | Session hook that delivers waiting mail into a conversation, and wakes a Claude or Codex turn that was about to end. Speaks all three CLIs' hook dialects. |
| `watcher.py` | Observes waiting mail without consuming it. Silent by default; desktop notifications and terminal bells require explicit flags. |
| `shell.sh` | Shadows `claude`/`codex`/`gemini` so a bare start briefs the session. `bmail`, `bwatch`. |
| `*_hooks_snippet.json` | Hook config to install into each CLI. |

## Tools each agent gets

`whoami`, `list_agents`, `set_name`, `set_job`, `send_message`,
`broadcast_message`, `confirm_project`, `delegate_task`, `report_result`,
`receive_messages` (with `wait_seconds` to park inside a turn), `ack_message`.

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
that payload — Codex rejects any key its schema does not name. Hooks do
not emit terminal bells or generic system-message notifications.

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

An empty inbox check spends no budget. Receipts alone do not continue a
turn; they remain available to the next ordinary read. If a receipt backlog
precedes a message needing attention, that message and the preceding
receipts are included in the same continuation.

`--no-wake` on `session_hook.py` delivers at the end of a turn without
continuing it, for anyone who wants the notification and not the autonomy.

## The window that is not looking

One gap survives all of that. Every hook is a reply to something the CLI
asked, so a window sitting at an empty prompt fires none of them: not
`PostToolUse`, because it is running no tools, and not `Stop`, because its
turn ended long ago. Its mail waits for the operator to type something.

`watcher.py` is one background process per window, started by
`session_hook.py` at `SessionStart`. It observes waiting mail and is
**silent by default**. Desktop notifications require `--notify`; terminal
bells require `--bell`. Neither can deliver mail into a conversation or
start a turn.

An idle window cannot be woken through the available hook interface:

| Route into an idle window | Why not |
| --- | --- |
| Keystroke injection (`TIOCSTI`) | `dev.tty.legacy_tiocsti = 0` on this kernel, and on most since 6.2 |
| Writing to the window's stdout | Paints characters over a TUI that is redrawing. A bell is the exception: no glyph, so nothing to corrupt |
| The hook interface | A reply to a question the CLI asked. Not a door to knock on |
| `tmux send-keys` | Genuinely works — and needs every CLI launched inside tmux, which is a different decision than this one |

The operator presses Enter and the ordinary hooks deliver. Optional
notifications can prompt that action, but are disabled unless explicitly
requested.

### Two rules it must not break

**It never consumes.** It looks with `bus.peek`, which scans without moving
the cursor and without settling delivery, so the mail it observes is
still there for the window's own hook. A watcher that read the message
would be a watcher that stole it.

**It never touches presence.** `bus.touch` here would refresh the heartbeat
of a window doing nothing, so the roster would show every watched window as
permanently online despite being unable to read. Presence has to keep
meaning "a hook fired recently"; an idle window can still be selected for
a project check without pretending it has recently been active.

### The details that matter

- **One per window**, held by an exclusive lock on
  `state/watch.<agent>.<session>.lock`. `SessionStart` fires again on resume
  and on clear; the second watcher takes one look at the lock and exits.
- **It dies with its window.** It follows the CLI's pid and exits when that
  process goes, so closing a terminal takes its watcher with it.
- **A five second grace for optional notifications.** A window that is
  mid-task usually collects its own mail on the next `PostToolUse` before
  a notification becomes eligible.
- **Session and cwd are passed in, not guessed.** The child is detached into
  its own session group, so by the time it looks its parent is init.
- `AGENTBUS_WATCHER=0` starts no watcher. Without flags, the watcher sends
  no desktop notifications or terminal bells. `--notify` and `--bell`
  independently enable them; the older `--no-notify` flag still disables
  desktop notifications even alongside `--notify`.
- `AGENTBUS_WATCHER_LOG=<file>` enables diagnostic logs. The detached
  process does not print to its inherited descriptors.

## Requirements

**Redis is required.** It must be installed and running before the bus is
useful.

```bash
sudo apt install redis-server      # Debian / Ubuntu
python3 -m pip install -r requirements.txt
redis-cli ping                     # expect: PONG
```

Messages themselves live in the file, not in Redis. What Redis carries is
the doorbell: a ring, with no contents, saying *look at the bus*. That
sounds minor and is not, because a blocking subscribe is the only thing
that reaches a window nobody is typing into. A window sitting at an empty
prompt fires no hooks, so without the doorbell it learns about mail only
when its operator next presses a key.

Point `AGENTBUS_REDIS_URL` elsewhere to use a different server, which is
also how windows on two machines reach each other. The default is
`redis://127.0.0.1:6379/5` — database 5 to stay out of the way of
anything else on that server.

The database number is a courtesy, not isolation: Redis pub/sub is not
scoped per database, so channels are visible across all of them whatever
number you choose. What actually keeps the bus from colliding with
another application is that every channel is named `agentbus:...`.

If Redis is missing the bus falls back to polling instead of failing, so
nothing is lost and no message goes astray. That fallback exists so a
broken Redis cannot take the bus down with it -- not as a supported way
to run. Delivery to an idle window stops working without it.

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

## Every window has a name

A window that never named itself used to publish as its CLI plus four
characters of its session id — `claude-0d1c`. Unique, and it told nobody
anything, so the roster filled with windows you could address but not
choose between.

Now a window is named from its job at SessionStart:
`agentbus@numbered-window-names` becomes `claude-numbered-window-names-001`,
and the next window on that branch becomes `-002`. The branch is preferred
over the repository, because two windows on one repository is the ordinary
case and two on one branch is the thing worth telling apart.

It is a default, not a policy. A window that names itself keeps that name,
and `$BUS name claude-<task>` still renames one at any point. A job that
says nothing leaves the old fallback rather than inventing something.

## A send says whether it landed

A send used to return the moment the question was asked, which told the
sender nothing about whether anybody answered it. That matters more for an
agent than it would for a person: it has no terminal to watch and no
reason to look again, so silence reads as success and a message that was
never delivered looks exactly like one that was.

Now it waits briefly and says:

```
codex-bus-check-001 -> claude-fbtest-001 (awaiting_confirmation)
claude-fbtest-001: confirmed, message delivered
claude-fbtest-001: declined the project, nothing was shared
claude-fbtest-001: no answer yet, nothing shared so far
```

Bounded on purpose — a courtesy at the end of a send, not a reason for the
shell to hang. `AGENTBUS_SEND_WAIT` sets the budget in seconds (default 12,
`0` to return immediately as before). Whatever is undecided by the deadline
is reported as undecided.

## Watching and poking it by hand

```bash
bwatch                                   # tail every message, live
bmail                                    # who is online
bmail send claude codex "text"           # related window; confirm project first
bmail broadcast claude codex "text"      # confirmation per Codex window
bmail confirm codex <confirmation-id> yes # only if this is your project
bmail read codex                         # read as codex
cat /tmp/agentbus/bus.jsonl              # it is just a file
```

`bwatch` tails from the end of the file and keeps no cursor, so watching
never affects what an agent receives.

## A message leaves when it has been read

A released message is finished when its chosen session reads it. The
chosen session stays the recipient even if its name changes. Content held
for project confirmation has not yet been delivered and cannot be consumed
by reading the question.

Broadcasts perform this check independently for each recipient. One
window's confirmation and read do not deliver or consume another window's
copy. Each session has its own cursor, and finished messages stop being
carried; a window that starts later does not receive already finished
conversations.

An idle roster entry can be chosen for a project check. It still needs to
read and answer before any held content is released. A confirmation that
expires without an answer does not deliver the details later.

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

## Testing delivery

```
python3 -m unittest discover -v
```

The regressions use temporary bus directories. They cover names across
separate Codex commands, hook/tool identity sharing in Claude, related-window
routing, project confirmations and explicit broadcasts, preservation of
unread mail during identity migration, roster counts, and continuation
budgets. They do not start live agents or
send mail to the real bus.

## Linting

Install the development tools and run the local style checks:

```bash
python3 -m pip install -r requirements-dev.txt
./linit.sh
```

The script removes unused imports, sorts imports, and formats Python files
in place before running Flake8, Google-style docstring checks, and Pylint.
Pass file paths to limit a run, for example `./linit.sh bus.py`.

## License

MIT — see [LICENSE](LICENSE).
