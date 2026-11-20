# Design notes

This document explains how goal-full is put together and why. It is aimed at
someone who wants to modify the plugin or understand the trade-offs before
relying on it. The README covers what it does; this covers the reasoning.

## The constraint: a plugin, not a fork

goal-full exists because Hermes already has a goal engine. `/goal` stores a
standing objective, `/subgoal` adds acceptance criteria, `/goal gate add` adds a
shell command that must exit 0, and after every turn an auxiliary judge decides
whether the goal is done. That engine is good. The gap was the setup: the user
had to write the contract, the steps, and the gates, one command at a time, or
accept a contract-only draft from `/goal draft`.

Everything in goal-full is plugin code. It imports Hermes' own goal storage and
command dispatcher rather than reimplementing them, and it adds no fields to
`GoalState`. That keeps the stored goal readable by every built-in command
(`/goal show`, `/subgoal`, `/goal gate`) and by the loop itself.

The cost of that decision is a private import surface. See
[Compatibility](../README.md#limitations-and-compatibility).

## Two entry points

### `/goalfull <task>` (human-initiated)

1. `dispatch_goal_command(scratch, "draft <task>")` runs Hermes' own draft path
   against a throwaway `GoalManager` whose session id is suffixed
   `:goalfull-preview`. Nothing the draft writes can reach the real session.
2. A second auxiliary call asks the same `goal_judge` routing for 3-6 subgoals
   and any shell gates the objective implies. The reply is parsed leniently:
   fenced JSON, trailing prose, or a junk reply degrades to "no plan", never an
   exception.
3. The drafted contract, subgoals, and gates are rendered and shown. The user
   picks **Approve and start** or **Reject, store nothing**. No answer stores
   nothing.
4. On approval the same objects are written to the real session through
   `GoalManager.set`, `add_subgoal`, and `add_gate`, then the kickoff turn is
   injected. The stored contract is the one that was shown; there is no second
   judge call that could silently change it.
5. The kickoff text carries the approved steps and tells the agent to write them to the todo list.

Drafting into a scratch manager is the important part. It means a rejected plan
leaves no trace, and an approved plan is exactly what the user read.

### `goal_update` (agent-initiated)

A tool handler receives the calling session's id, so it can build a
`GoalManager` for the live session and write mid-turn. On the gateway, Desktop,
and TUI, the post-turn hook loads a fresh manager and picks the goal up without
a kickoff message. That is why the agent can start a goal itself.

Actions:

| Action | Guard |
| --- | --- |
| `start` | Refuses if a goal is already active; budgets 10 turns by default |
| `add_criterion` | Duplicate-safe; hard cap of 10 criteria |
| `add_gate` | Hard cap of 6 gates; timeout clamped to 120 s |
| `show` | Read-only |
| `remove_criterion` / `remove_gate` / `edit_criterion` | Reason required, judge approval required, per-goal caps |

`start` never replaces an active goal because `GoalManager.set` would silently
overwrite one. The refusal returns the current goal and budget so the agent can
extend it instead.

## Why removals are judged

The failure mode this plugin is built around is an agent that narrows the finish
line until it can reach it. If the agent could delete a failing criterion, every
goal would converge on success. Refusing all removals is also wrong: criteria go
stale, gates target the wrong path, and a human then has to clean up.

The middle path: the agent may propose a removal or edit, but it must give a
reason, and the goal judge must approve. The judge prompt says to reject when in
doubt and to approve only when the item is redundant, wrong, impossible, or out
of scope. A judge failure, timeout, or malformed reply is a rejection. This is
deliberately fail-closed.

Caps bound how much the agent can talk itself into: three removals and three
edits per goal, tracked in a sidecar keyed by the goal's `created_at`. A new goal
starts at zero. Every decision is appended to `audit.jsonl` under
`$HERMES_HOME/state/goalfull/`.

The user's own `/subgoal remove N` and `/goal gate remove N` are the unjudged
path. The tool description tells the agent to point the user there rather than
argue with the judge.

## Delegation: carry the finish line, judge the result

A delegated subagent does not see the parent's goal. Two hooks close that gap:

1. `pre_tool_call` on `delegate_task` prepends a compact finish-line block (goal,
   numbered criteria, gate commands) to every task brief and to the context when
   one is present. It skips `action="continue"` so a re-dispatch does not stack
   duplicate blocks. It fails open: a storage error means the brief is sent
   unchanged.
2. `transform_tool_result` on `delegate_task` extracts the completed summaries
   from a blocking result and asks `judge_goal` whether they satisfy the parent's
   goal. Accepted results get a one-line confirmation appended. Rejected results
   are replaced with an instruction to re-dispatch with `action="continue"` and
   the same task goal, so the child keeps its transcript. Retries are capped at
   two per delegation, reset on a fresh spawn.

Both hooks do nothing when no goal is active, when the result is a background
acknowledgement, or when the judge reply cannot be parsed. Failing open here is
intentional: a broken judge should not break delegation. The worst case is that
a weak summary gets through, which is the behaviour without the plugin.

## State layout

```
$HERMES_HOME/state/goalfull/
  <session>-<hash>.json   per-goal counters (removals, edits, delegation retries)
  audit.jsonl             append-only decision log
```

The sidecar is keyed by a sanitized session id plus a short hash, and it stores
the goal's `created_at`. If the stored timestamp does not match the live goal,
the counters reset. Writes are best-effort: a failed audit write is logged, not
raised, because losing an audit line must not block the user's goal.

## What Hermes allows, and what it does not

- A plugin can register commands, tools, and hooks. It cannot add fields to
  `GoalState`, so per-criterion done/not-done state is not available. Subgoals
  are acceptance criteria, not a checklist with statuses.
- The classic in-process CLI caches one `GoalManager` per session. A tool write
  that lands mid-turn can be overwritten by that cached manager's next save.
  Gateway, Desktop, and TUI build a manager per action and do not have this
  problem. Desktop is the supported path.
- The judge model is Hermes' `goal_judge` auxiliary. goal-full does not ship a
  model and does not route around that setting.
- Gates run as shell commands in the session's working directory, with the
  user's permissions. A gate is not a sandbox.

## Rejected designs

- **A hook that rewrites prompts instead of a tool.** A hook can add text, but it
  cannot store a criterion the judge will read. Durability required a tool.
- **Inferring the goal from the conversation.** Silent goal creation is the
  opposite of review-before-acting. The agent can start a goal explicitly, with
  a guard against replacing one; it does not guess.
- **A second judge.** Hermes already judges each turn. A separate judge would
  duplicate it and add latency.
- **Kanban as the task backbone.** Kanban is a multi-profile board with
  dispatcher and worker processes. It is the right tool for a fleet, and too
  heavy for one session's goal loop.
- **Per-subgoal state in the session store.** Not possible without a core change,
  and a core change was out of scope.

## Testing

The tests run the real Hermes goal storage against a temporary `HERMES_HOME`.
Model calls are replaced with fixed replies or injected callables, so the suite
needs no network and no running gateway. The two hooks are tested with synthetic
`delegate_task` results. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the
command.
