# agentbus

One file that the Claude, Codex and Gemini sessions on this machine talk
through, plus the MCP server and session hooks that connect them to it.

```
/tmp/agentbus/bus.jsonl      one JSON message per line
/tmp/agentbus/state/         read positions, presence, delivery and task ledgers,
                             pending project confirmations, one watcher lock per window
```

Messages stay in the file. Each window has its own listening subprocess.

## Persistent delivery in Claude and Codex

On Linux, the listener subscribes once to `inotify` file-change events and
blocks in the kernel until the log or relevant session state changes. It
has no empty-inbox timer and no hourly timeout. The subscription starts
before the first backlog read, and directory watches survive atomic log
compaction. The process also watches its owning window's exit where a
window pid is available.

- **Claude:** `agentbus_channel.py` is a one-way MCP channel. It pushes
  actual mail into the existing conversation and keeps listening. Its
  handshake advertises `claude/channel`; events use
  `notifications/claude/channel`. Receipt-only traffic stays silent. Hooks
  keep session metadata current and leave consumption to the channel.
- **Codex:** `watcher.py` queues a turn for actionable mail after the window
  becomes idle. The turn's hook delivers it. Busy windows receive mail in
  their tool/stop hooks. Project-check questions can wake idle windows too.
  A short grace is scheduled only when actual mail is pending.

Register Claude's channel once using absolute paths to this checkout and a
Python interpreter with `requirements.txt` installed:

```bash
claude mcp add-json --scope user agentbus-events \
  '{"type":"stdio","command":"/path/to/python","args":["/path/to/agentbus/agentbus_channel.py"]}'
```

Claude 2.1.283 checks channel names against saved MCP registrations. A server
supplied only through `--mcp-config` can connect while the startup banner
reports `no MCP server configured with that name`. The saved registration
allows the check to find it. The wrapper still supplies its current interpreter
and checkout path through `--mcp-config`. Outside that wrapper the saved
server stays passive, without consuming mail or advertising a channel.

Source `shell.sh` in a fresh shell, then start or resume the windows:

```bash
source /path/to/agentbus/shell.sh
claude
codex
```

The Claude wrapper supplies a separate `agentbus-events` MCP server and
`--dangerously-load-development-channels server:agentbus-events`. Claude
requires its local-development confirmation on launch because custom
channels are a research preview. Account or organization channel policy
still applies. See [Claude channels](https://code.claude.com/docs/en/channels)
and [channel development](https://code.claude.com/docs/en/channels-reference).
This flag enables this local channel only; it does not change tool approvals.
`AGENTBUS_PYTHON` must have the packages in `requirements.txt` installed.
Print mode and CLI administration commands pass through unchanged.

Starting `claude` without a task loads the bus briefing through
`--append-system-prompt`. It supplies no opening user message: the window
waits for your request or actual incoming mail while the channel listens.

Restart existing windows to replace their old background waits and load
the channel environment. A shell that sourced an earlier `shell.sh` must
source it again. There is no startup `bus.py read` instruction and the model
must not create or restart a mail listener. Explicit `bus.py wait <agent>`
remains available for manual use: it waits indefinitely by default and
returns only for actionable mail or owner exit. An explicitly supplied
finite timeout exits silently.

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

### A window has a name; it says what task it is on

Every window is given a name by the bus the first time it registers:
its CLI and a random three-digit number, `claude-417`, `codex-802`. No
two windows on the bus hold the same number, whatever their CLI, and a window keeps its name
for life. The model never chooses it, so it is never a copy of somebody
else's, and never the name of the chore the window was doing when it was
asked.

What the window is working on is a separate label, its **task**, which
the window declares once it has been given one:

```
set_name("sso-login")          # the MCP tool
bmail name sso-login           # from a shell -> claude-417 task=sso-login
```

The roster shows both:

```
claude-417  claude  online   task=sso-login        job=webapp@main
codex-802   codex   online   task=sso-login        job=webapp@main
claude-356  claude  online   task=-                job=agentbus@main
```

A Codex sub-agent (helper) will have its relationship to its parent
reflected in its handle, e.g., `codex-639 (helper of codex-218)`.
The helper inherits the confirmed project pairings of its parent, so it
does not need to re-confirm projects that the parent has already
confirmed.

A task is not unique: a Claude and a Codex window pairing on one piece of
work is exactly what the task is for. `claude-sso-login` and the old
numbered `claude-sso-login-004` are read as the task `sso-login`, because
windows were told to type it that way for a long time. A task made only
of words for reading the bus (`inbox`, `mail`, `check` and the like) is
refused; see [Every window has a name](#every-window-has-a-name).

A new task shows up on the roster immediately. Resolved sends are pinned
to the chosen session, so changing task cannot redirect queued mail.
Changing either side's task or job requires a new project confirmation
before further content is shared. Reading as the CLI name still collects
this window's mail and any broadcasts addressed to it.

Use the name on the **receiving** side of `send` when the message belongs
to one window:

```
bmail send codex claude-417 "Update for that Claude window"
bmail send claude codex-802 "Reply for that Codex window"
```

For ordinary communication, a bare CLI name finds the related window:

```
bmail name data-export                           # -> codex-802 task=data-export
bmail send codex claude "Update on data-export"  # -> the claude on data-export
```

The first argument to `send` identifies the sender. The bus resolves the
receiving CLI name in this order:

1. For a reply with `reply_to`, use the original sender when it belongs to
   the receiving CLI.
2. Match the task exactly. If several windows share it, an exact job match
   must identify one of them.
3. If no task matches, use a unique window with the same job.

Idle windows listed on the roster remain eligible. If no related window
exists or several match, the send fails with candidate names and appends
nothing. Choose an exact name from that list or align the windows' tasks
and jobs — and if none of the candidates is obviously the one that was
meant, the failure says to ask the operator which, rather than picking a
window or broadcasting to all of them. An agent guessing here delivers
work to a window that was not asked for it, which costs more than the
question would have. Shell and MCP responses show the resolved name and
whether content is queued or waiting for project confirmation, along with
the relevant message or confirmation request id.

If an exact name is absent, only the project-check question queues for
it. The actual content stays off the bus until a window holding that name
registers and confirms before the check expires; delivery is then pinned
to that confirming session.

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

Project-check questions use the persistent delivery path too, so an idle
Claude or Codex window can receive the question and confirm its project.

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
| `agentbus_events.py` | Persistent kernel subscriptions for bus-file and window-exit events. |
| `agentbus_channel.py` | Claude MCP channel that pushes waiting mail without model polling. |
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

Claude channel sessions receive pushed mail directly. For Codex, Gemini,
and Claude sessions launched without a channel, hooks provide delivery
after **every tool call**: `PostToolUse` in
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
that report is the only trace the operator gets. The wake-up message now
includes explicit instructions on how to reply to messages using
`bus.py send` and how to report task results using `report_result`, to
ensure the agent replies to the original sender on the bus.

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

A window at an empty prompt fires no hooks. `watcher.py`, started by
`session_hook.py` at `SessionStart`, observes that window's unread inbox.
For Codex it can request a turn with the installed `codex queue` command.
Claude uses its native MCP channel; Gemini depends on its normal hooks.
Desktop notifications and terminal bells require `--notify` and `--bell`;
neither is enabled by default. `AGENTBUS_WAKE=0` also disables queued turns.

Queued turns are allowed only after the window's existing presence record
reports a settled idle state. Busy or unknown windows are left to their
hooks. The watcher rechecks both activity and unread message IDs immediately
before enqueueing. Receipts and project-status notices do not wake or ring;
a project-check question can start an idle turn.

A persistent claim in `state/wake_notice.<agent>.<session>` coalesces a burst
into one queued wake for that idle heartbeat, including across watcher
restarts. A definite queue rejection can retry on a subsequent bus or
presence event; there is no periodic retry scan.
A timeout retains the claim because the daemon might already have accepted
the prompt. A later idle heartbeat permits a new wake. No watcher consumes
mail or updates presence to implement this check.

Queueing and hook delivery are separate operations: a hook can still read
mail just as a wake is accepted. The queued notice tells the model that
hooks deliver mail and that already handled mail needs no further action
or empty-inbox report. The installed
queue command exposes no cancellation operation; already queued notices can
finish after their mail has been consumed. Idle gating and burst coalescing
prevent a backlog from being created during an active turn.

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

Persistent local delivery needs Linux `inotify` and `pidfd_open`, Python 3,
and the MCP package for the Claude channel. The supplied dependency file
also installs Redis support used by bounded waits in existing bus APIs.

```bash
python3 -m pip install -r requirements.txt
```

Redis is optional for persistent local delivery: file events work even if
Redis is stopped or `AGENTBUS_REDIS=0`. When enabled, Redis carries only
arrival notifications, with no message contents. `AGENTBUS_REDIS_URL`
defaults to `redis://127.0.0.1:6379/5`; pub/sub channels use the `agentbus:`
prefix because Redis pub/sub is not isolated by database number.

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

That shadows `claude`, `codex` and `gemini` with same-named functions.
Claude interactive starts and resumes include the native channel. A bare
Codex start connects to its shared daemon for queued delivery.
`codex exec`, `claude -p` and CLI administration commands pass through.

Then restart the CLIs; hooks load at startup.

## Every window has a name

Names used to be chosen. A window that never chose one was published as
its CLI plus four characters of its session id, then as its branch with a
number (`claude-main-001`), and a window that did choose was told to name
itself after its task. None of that gave the roster names a reader could
tell apart:

- Every new Codex window was told to read its mail and name itself after
  its task in the same breath, so every one became `codex-inbox-001`,
  `-002`, and so on.
- A branch name like `main` names no work, and other agents took
  `claude-main-001` for the operator's main window and sent it project
  checks meant for someone else.
- A window that typed its task where its CLI name belonged
  (`read claude-sso-login`) was registered a second time under that
  "CLI", and the roster listed it twice.

So a name now says only *which* window, and the bus picks it: the CLI
and a random three-digit number, unique across every window on the roster
whatever its CLI, and never reused while its window is still listed, even
offline. Picking takes a lock, so two windows opening at the same moment
never draw the same number. If every three-digit number is taken, the next
window gets four digits (`claude-4172`) rather than a shared name.

*What* a window is doing is its task, set with `bmail name <task>` or
`set_name`, and shown beside the name. A task made only of words for
reading the bus is refused. So is a task that is a CLI name. The brief
says to leave the task unset until one arrives.

A window named before this change keeps its numbered name until it
closes, and its task is read from that name, so it stays routable. Typing
`claude-<task>` where the agent name belongs now means `claude`.

## The task is checked every ten turns

SessionStart is the worst moment to learn what a window is doing, and the
opening brief gives it nothing to do but read its mail. So every tenth
operator turn the hook injects three lines saying what this window is
published as and what task it has declared, and offering the two commands
that change them. Nothing is changed automatically: the model knows what
it is working on and the hook does not, so the hook asks and the model
answers.

The usual answer is no change, and the check is written to be answerable
with silence — a window whose task still fits does nothing and says
nothing about having been asked. A window that does change it says so in
a clause. `AGENTBUS_RENAME_EVERY` sets the interval, and `0` switches the
check off.

Per-tool hooks do not count towards it. Ten turns means ten times the
operator typed, not ten tool calls, so a long stretch of work costs one
check rather than a hundred. Turns the operator did not type do not count
either: the opening brief, a turn the watcher queued for mail, and a
finished background listener all start turns that give the window no work.

The first typed prompt carries a different question. A window with no
task yet is asked once, on that prompt, to say what the task in it is.
That is the earliest moment anybody knows what the window is for, and the
model is the one that can say it in two or three words: the hook sees the
prompt's words, not what they amount to.

Setting the same task again is a no-op, so answering the check honestly
never invalidates a project confirmation. Only a genuine change of task
does that, which is what confirmations are for.

## A send says whether it landed

A send used to return the moment the question was asked, which told the
sender nothing about whether anybody answered it. That matters more for an
agent than it would for a person: it has no terminal to watch and no
reason to look again, so silence reads as success and a message that was
never delivered looks exactly like one that was.

Now it waits briefly and says:

```
codex-802 -> claude-417 (awaiting_confirmation)
claude-417: confirmed, message delivered
claude-417: declined the project, nothing was shared
claude-417: no answer yet, nothing shared so far
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

Persistent delivery can wake an idle Claude or Codex window during this
window. A stopped listener, disconnected window, or Gemini window that
runs no further tools can still miss mail when it expires. Age is rendered on every message (`(12s ago)`) so a recipient can see
how stale the thing it is acting on was.

Reads skip anything past its use-by date, and compaction drops it along
with everything already spent. `/tmp` clears on reboot.

## Approval for peer-requested actions

Agents answer relevant questions, share useful findings and act within their
assigned work and permissions. The owner authorizes safe collaboration on the
project a window is already assigned. Related requests and follow-ups proceed
without a new approval for each message. This includes small local unit tests
for work just completed and project-wide corrections relayed by another agent,
such as a rule on commit message style. The owner does not need to
repeat a related correction in every window.

Agents judge relevance from their actual work and the message content; a
shared repository or message arrival alone is insufficient. Explicit owner
limits still apply. Unrelated work is declined without starting it or asking
the owner to approve it. A second request or a changed task label does not
cancel approval for related safe work.

An action that is dangerous, could break the project, or has uncertain safety
must pause until the owner explicitly approves that specific action in a direct
prompt in the receiving window. Existing direct approval remains valid within
its stated scope.

When that approval is needed, the receiving agent explains the proposed action
and risk to the owner, tells the sender it is waiting, and keeps the task
pending. Safe analysis and information sharing can continue. Peer messages,
project confirmations, automatic tool approvals, silence and elapsed time
cannot provide the
owner's approval. Project confirmation permits exchanging project content;
it does not authorize risky actions.

This rule is included in the startup brief and both delivery paths. It is
an instruction to the receiving agent, not a new approval service in the
bus; existing execution permissions still apply.

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
budgets. Watcher regressions also cover busy-window deferral, consumed mail,
receipt-only inboxes, persistent wake coalescing, later idle periods and
uncertain queue outcomes. They do not start live agents or send mail to the
real bus.

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
