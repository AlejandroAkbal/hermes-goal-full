# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries before 0.4.0 predate this repository and are summarised from the project's
design notes, so they carry no exact date.

## [0.4.0] - 2026-11-20

### Added

- `goal_update` action `start`: the agent can start a goal for a big request without a slash command.
  Refuses when a goal is already active. Default budget is 10 turns, configurable with
  `goalfull.agent_started_max_turns`.
- Judged mutation: `remove_criterion`, `remove_gate`, and `edit_criterion` require a reason and the goal
  judge's approval. Caps of 3 removals and 3 edits per goal. Every decision is written to
  `$HERMES_HOME/state/goalfull/audit.jsonl`.
- `pre_tool_call` hook: appends the active goal's finish line to every `delegate_task` brief.
- `transform_tool_result` hook: judges completed subagent summaries against the parent goal and asks for
  a bounded re-dispatch when they fall short. Two retries per delegation, configurable.
- `goalfull` config section: `agent_started_max_turns`, `require_approval` (reserved),
  `max_removals_per_goal`, `max_edits_per_goal`, `delegation_judge_retries`.

### Changed

- The `goal_update` tool description now tells the agent when to start a goal and that removals are
  judged.

## [0.3.0]

### Added

- `goal_update` tool: `add_criterion`, `add_gate`, and `show`, so a running goal can grow without the
  user typing a command.
- Criteria cap of 10 and gate cap of 6. Gate timeouts are clamped to 120 seconds.

### Changed

- The kickoff text now carries the approved subgoals and asks the agent to seed the todo list.

## [0.2.0]

### Added

- Review step: the drafted goal is shown before anything is stored, with **Approve and start** and
  **Reject, store nothing**.
- Drafting happens in a throwaway session, so a rejected plan leaves no trace.

### Fixed

- The plan draft no longer blocks the contract when the auxiliary model fails; the goal is set as a
  free-form goal with a note.
- Approval is required; if the choice prompt is unreachable, nothing is stored.

## [0.1.0]

### Added

- `/goalfull <task>`: drafts a completion contract, 3 to 6 subgoals, and shell gates from one sentence,
  then stores them through Hermes' goal API and starts the loop.

[0.4.0]: https://github.com/AlejandroAkbal/hermes-goal-full/releases/tag/v0.4.0
[0.3.0]: https://github.com/AlejandroAkbal/hermes-goal-full/releases/tag/v0.3.0
[0.2.0]: https://github.com/AlejandroAkbal/hermes-goal-full/releases/tag/v0.2.0
[0.1.0]: https://github.com/AlejandroAkbal/hermes-goal-full/releases/tag/v0.1.0
