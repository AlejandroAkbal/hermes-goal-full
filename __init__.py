"""goal-full: one slash command sets a Hermes goal completely.

`/goalfull <task>` drafts a completion contract plus the subgoals and gates that
`/goal draft` never writes, stores all three through the same goal API the
built-in commands use, and queues the kickoff turn so the loop starts.

Nothing here touches Hermes source. The command is registered through the
plugin host (`PluginContext.register_command`), the contract comes from
`hermes_cli.goals.draft_contract`, and the stored state is written by
`GoalManager.set`, `add_subgoal`, and `add_gate` — the same calls `/goal`,
`/subgoal`, and `/goal gate add` make.

The plan is drafted by the configured `goal_judge` auxiliary model, which only
ever fills the five contract fields. One extra call with the same routing asks
for the subgoals and gates. That call is injectable (`plan_call`) so the
command can be tested without a model.

The `goal_update` tool writes with one read-modify-write on a fresh
`GoalManager`. On the classic in-process CLI the loop caches one manager per
session (`hermes_cli/cli_loops_mixin.py:577-597`) and its next `_save()` can
overwrite a mid-turn addition; the gateway/Desktop/TUI build a manager per
action (`gateway/run_goals.py:88-95`, `tui_gateway/prompt_turn.py:67-78`), so
Desktop is the supported path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

COMMAND = "goalfull"

_MIN_SUBGOALS = 3
_MAX_SUBGOALS = 6

_PLAN_SYSTEM_PROMPT = (
    "You break a user's objective into the steps and checks an autonomous coding "
    "agent needs. Reply ONLY with one JSON object on one line, no prose:\n"
    '{"subgoals": ["..."], "gates": [{"command": "..."}]}\n\n'
    "subgoals: three to six short steps, in the order they can be done, each one "
    "sentence naming the concrete thing it produces.\n"
    "gates: shell commands whose exit code proves the work, each run from the "
    "project root. Include one only when the objective names something a command "
    "can check, such as a test, a build, or a lint. Use an empty list when nothing "
    "named can be checked by a command. Never invent a command the objective does "
    "not imply."
)

TOOL_NAME = "goal_update"
_MAX_CRITERIA = 10
_MAX_GATES = 6
_MAX_GATE_TIMEOUT = 120

_GOAL_UPDATE_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Record work toward a goal and manage it. While a goal is active you may add criteria (add_criterion)\n"
        "or shell gates (add_gate) as you discover more of the finish line, and re-read it (show).\n"
        "\n"
        "START GOALS YOURSELF: when the user's request is a big task — roughly 3+ distinct steps, multiple\n"
        "files, 5+ tool calls expected, or long-running — call action 'start' with a one-line objective. The\n"
        "goal, its criteria and its gates are drafted for you; you then work the goal loop until the judge\n"
        "accepts. Never start a goal when one is already active (extend it with add_criterion/add_gate\n"
        "instead) and never start one for trivial single-step asks.\n"
        "\n"
        "REMOVALS ARE JUDGED: remove_criterion / remove_gate / edit_criterion go through the goal judge. You\n"
        "must give a reason; a judge that cannot confirm the change rejects it and nothing changes. The user's\n"
        "own commands (/subgoal remove N, /goal gate remove N) are the unjudged path — if the user wants a\n"
        "removal, say so and let them run it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "add_criterion", "add_gate", "show",
                                                  "remove_criterion", "remove_gate", "edit_criterion"]},
            "objective": {"type": "string", "description": "one-line objective, for start"},
            "max_turns": {"type": "integer", "description": "turn budget, for start; defaults to the configured agent budget"},
            "text": {"type": "string", "description": "the criterion, for add_criterion"},
            "command": {"type": "string", "description": "shell command run from the project root, for add_gate"},
            "timeout_seconds": {"type": "integer", "description": "for add_gate; clamped to 120"},
            "index": {"type": "integer", "description": "1-based criterion/gate number, for remove_criterion/remove_gate/edit_criterion"},
            "reason": {"type": "string", "description": "why the change keeps the finish line honest; required for removals/edits"},
            "new_text": {"type": "string", "description": "replacement criterion text, for edit_criterion"},
        },
        "required": ["action"],
    },
}

_USAGE = "Usage: /goalfull <what should be true when this is done>"

_NO_APPROVAL = (
    "goalfull: Hermes's approval prompt is not reachable from here, so nothing was stored. "
    "Run /goalfull from a Hermes session."
)

# The PluginContext Hermes hands to register(); Hermes 0.23 removed the module-level
# accessor that older releases exposed, so the context is kept here instead.
_CTX: Any = None


def _plan_prompt(task: str) -> str:
    return f"Objective:\n{task.strip()}"


def _parse_plan(raw: str) -> tuple[list[str], list[str]]:
    """Pull subgoals and gate commands out of a model reply. Junk is dropped, never raised."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return [], []
        try:
            data = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return [], []
    if not isinstance(data, dict):
        return [], []

    subgoals = [str(s).strip() for s in data.get("subgoals") or [] if str(s).strip()]
    gates: list[str] = []
    for gate in data.get("gates") or []:
        command = gate.get("command") if isinstance(gate, dict) else gate
        command = str(command or "").strip()
        if command:
            gates.append(command)
    return subgoals, gates


def _clamp_subgoals(subgoals: list[str]) -> tuple[list[str], str]:
    """Enforce the promised range: extras are dropped, a short plan gets a one-line note."""
    kept = list(subgoals)[:_MAX_SUBGOALS]
    if len(kept) >= _MIN_SUBGOALS:
        return kept, ""
    return kept, (
        f"Note: the plan has {len(kept)} subgoal(s); goal-full expects "
        f"{_MIN_SUBGOALS}-{_MAX_SUBGOALS}."
    )


def _default_plan_call(task: str) -> str:
    """The plan draft, routed through the same `goal_judge` auxiliary the contract uses."""
    from agent.auxiliary_client import call_llm

    from hermes_cli.goals import _call_goal_judge_llm, _goal_judge_timeout

    return _call_goal_judge_llm(
        call_llm, _PLAN_SYSTEM_PROMPT, _plan_prompt(task), _goal_judge_timeout(),
    )


def _render(state: Any, subgoals: list[str], gates: list[str]) -> str:
    lines = [f"Goal set: {state.goal}"]
    if state.has_contract():
        lines += ["", state.contract.render_block()]
    if subgoals:
        lines += ["", "Subgoals:"] + [f"  {i}. {text}" for i, text in enumerate(subgoals, 1)]
    if gates:
        lines += ["", "Gates:"] + [f"  $ {command}" for command in gates]
    return "\n".join(lines)


def _seed_text(subgoals: list[str]) -> str:
    if not subgoals:
        return ""
    steps = "\n".join(f"{i}. {text}" for i, text in enumerate(subgoals, 1))
    return ("\n\nYour approved steps, in order:\n" + steps +
            "\n\nStart by calling tool_search for todo_list, then write these as tasks and keep "
            "their status current. They are your working plan; the criteria and gates are the "
            "finish line. When you discover work the plan is missing, call goal_update.")


_APPROVE = "approve"
_REJECT = "reject"


def _summary_question(task: str, has_contract: bool, subgoal_count: int, gate_count: int) -> str:
    """The one-line question for the choice prompt; kept short so no UI truncates it."""
    task = task.strip()
    if len(task) > 80:
        task = task[:77].rstrip() + "..."
    fields = "5/5" if has_contract else "0/5"
    return (
        f"Start goal: {task} (contract {fields} fields, "
        f"{subgoal_count} subgoal(s), {gate_count} gate(s))?"
    )


def _review(
    preview: str,
    summary: str,
    approve: Optional[Callable[[str], Optional[str]]],
    notify: Optional[Callable[[str], Any]] = None,
) -> Optional[str]:
    """Ask before anything is stored. None means no one can answer, so nothing is stored."""
    if approve is None:
        return None
    preview = "This is the goal that would be set. Nothing is stored until you approve it.\n\n" + preview
    shown = False
    if notify is not None:
        try:
            shown = bool(notify(preview))
        except Exception as exc:
            logger.info("goal-full: posting the plan failed (%s)", exc)
        if not shown:
            logger.info("goal-full: plan not posted; including it in the question")
    return approve(
        summary if shown else summary + "\n\n" + preview,
        [
            {"value": _APPROVE, "label": "Approve and start"},
            {"value": _REJECT, "label": "Reject, store nothing"},
        ],
    )


def apply_plan(
    session_id: str,
    task: str,
    *,
    plan_call: Optional[Callable[[str], str]] = None,
    inject: Optional[Callable[..., Any]] = None,
    approve: Optional[Callable[[str], Optional[str]]] = None,
    session_key: str = "",
    notify: Optional[Callable[[str], Any]] = None,
) -> str:
    """Draft the contract, subgoals, and gates for *task*, then wait for approval.

    Nothing is stored until *approve* returns approval. On approval the three are written
    to *session_id* and, with *inject*, the kickoff turn is queued so the loop starts.
    """
    task = (task or "").strip()
    if not task:
        return _USAGE
    if not session_id:
        return "No session is attached to this command, so there is nowhere to store the goal."
    if approve is None:
        return _NO_APPROVAL

    from hermes_cli.goal_command import dispatch_goal_command
    from hermes_cli.goals import GoalManager, goal_kick_prompt

    plan_call = plan_call or _default_plan_call

    # Draft against a throwaway session so a rejected plan never touches the real one.
    scratch = GoalManager(session_id=f"{session_id}:goalfull-preview")
    result = dispatch_goal_command(
        scratch, f"draft {task}", authorize_gate=lambda: None, last_user_message=task,
    )
    if result.error or not scratch.has_goal():
        return result.output
    # What the user is about to approve is what gets stored: no second judge call.
    contract = scratch.state.contract if scratch.state.has_contract() else None

    subgoals: list[str] = []
    gates: list[str] = []
    try:
        subgoals, gates = _parse_plan(plan_call(task))
    except Exception as exc:  # the contract already stands; a failed plan must not undo it
        logger.info("goal-full: plan draft failed (%s)", exc)
    subgoals, range_note = _clamp_subgoals(subgoals)
    suffix = f"\n\n{range_note}" if range_note else ""

    summary = _summary_question(task, contract is not None, len(subgoals), len(gates))
    decision = _review(_render(scratch.state, subgoals, gates) + suffix, summary, approve, notify)
    scratch.clear()
    if decision != _APPROVE:
        reason = "rejected" if decision == _REJECT else "no approval was given"
        return (
            f"Nothing stored ({reason}).\n\n"
            + _render_preview(subgoals=subgoals, gates=gates, contract_output=result.output)
            + suffix
        )

    manager = GoalManager(session_id=session_id)
    manager.set(task, contract=contract)
    stored_subgoals = [manager.add_subgoal(text) for text in subgoals]
    stored_gates = [manager.add_gate(command).command for command in gates]

    note = ""
    if inject is not None:
        queued = False
        try:
            queued = bool(inject(goal_kick_prompt(manager.state.goal, task) + _seed_text(stored_subgoals), session_key))
        except Exception as exc:  # the goal is stored either way; say so instead of hiding it
            logger.info("goal-full: kickoff injection failed (%s)", exc)
        if not queued:
            note = "\nThe goal is stored, but the loop did not start. Send any message to start it."
    if contract is None:
        note += "\nNo contract was drafted (goal_judge unavailable) — running as a free-form goal."

    return f"{_render(manager.state, stored_subgoals, stored_gates)}{note}"


def _render_preview(*, subgoals: list[str], gates: list[str], contract_output: str) -> str:
    """The drafted plan as text, without a stored goal behind it."""
    lines = [contract_output, "", "Subgoals:"]
    lines += [f"  {i}. {text}" for i, text in enumerate(subgoals, 1)] or ["  (none)"]
    lines += ["", "Gates:"]
    lines += [f"  $ {command}" for command in gates] or ["  (none)"]
    return "\n".join(lines)


def _inject(ctx: Any) -> Callable[[str, str], Any]:
    """Queue *text* into the session the command was typed in.

    Keyed by the session key when the handler got one, because a keyless inject only reaches a
    local CLI session and is refused for the Desktop app and every other surface.
    """

    def inject(text: str, session_key: str) -> Any:
        if session_key:
            return ctx.inject_message(text, session_key=session_key)
        return ctx.inject_message(text)

    return inject


def _notifier(ctx: Any) -> Optional[Callable[[str], Any]]:
    """Post the plan into the session before the choice; False when it was not shown."""
    post = getattr(ctx, "post_message", None)
    if post is None:
        return None

    def notify(text: str) -> bool:
        try:
            handle = post(text)
        except Exception as exc:
            logger.info("goal-full: post_message failed (%s)", exc)
            return False
        return bool(getattr(handle, "ok", False))

    return notify


def handle(raw_args: str, session_id: str = "", session_key: str = "", **kwargs: Any) -> str:
    """The `/goalfull` handler. Hermes passes the session this command was typed in."""
    del kwargs
    ctx = _CTX
    inject = _inject(ctx) if ctx is not None and hasattr(ctx, "inject_message") else None
    approve = ctx.ask_choice if ctx is not None and hasattr(ctx, "ask_choice") else None
    return apply_plan(
        session_id, raw_args, inject=inject, approve=approve, session_key=session_key,
        notify=_notifier(ctx),
    )


def _goal_view(mgr: Any) -> str:
    """Goal, contract, criteria and gates as the agent needs to see them."""
    state = mgr.state
    lines = [f"Goal: {state.goal}", ""]
    lines.append(state.contract.render_block() if state.has_contract() else "Contract: none")
    lines += ["", "Criteria:"]
    lines += [f"  {i}. {text}" for i, text in enumerate(state.subgoals, 1)] or ["  (none)"]
    lines += ["", "Gates:"]
    lines += [f"  $ {gate.command}" for gate in state.gates] or ["  (none)"]
    return "\n".join(lines)


_GOALFULL_DEFAULTS = {
    "agent_started_max_turns": 10,
    "require_approval": False,
    "max_removals_per_goal": 3,
    "max_edits_per_goal": 3,
    "delegation_judge_retries": 2,
}


def _load_goalfull_cfg(cfg: Optional[dict] = None) -> dict[str, Any]:
    """The `goalfull:` config section over the defaults. Never raises."""
    out = dict(_GOALFULL_DEFAULTS)
    try:
        if cfg is None:
            from hermes_cli.config import load_config
            cfg = load_config()
        section = (cfg or {}).get("goalfull") or {}
        for key, default in _GOALFULL_DEFAULTS.items():
            if key not in section:
                continue
            try:
                out[key] = bool(section[key]) if isinstance(default, bool) else int(section[key])
            except (TypeError, ValueError):
                pass
    except Exception as exc:
        logger.info("goal-full: config read failed (%s)", exc)
        return dict(_GOALFULL_DEFAULTS)
    return out


def _goalfull_state_dir() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    path = (Path(home) if home else Path.home() / ".hermes") / "state" / "goalfull"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sidecar_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:64]
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
    return _goalfull_state_dir() / f"{safe}-{digest}.json"


def _fresh_sidecar(goal_created_at: float) -> dict[str, Any]:
    return {"goal_created_at": goal_created_at, "removals_used": 0, "edits_used": 0, "delegation_retries": 0}


def _load_sidecar(session_id: str, goal_created_at: float) -> dict[str, Any]:
    """Per-goal counters; a different goal (created_at) starts from zero. Never raises."""
    try:
        data = json.loads(_sidecar_path(session_id).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("goal_created_at") == goal_created_at:
            return {**_fresh_sidecar(goal_created_at), **data}
    except Exception:
        pass
    return _fresh_sidecar(goal_created_at)


def _save_sidecar(session_id: str, data: dict) -> None:
    try:
        _sidecar_path(session_id).write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.info("goal-full: sidecar write failed (%s)", exc)


def _reset_sidecar(session_id: str, goal_created_at: float) -> None:
    _save_sidecar(session_id, _fresh_sidecar(goal_created_at))


def _append_audit(row: dict) -> None:
    try:
        with (_goalfull_state_dir() / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **row}) + "\n")
    except Exception as exc:
        logger.info("goal-full: audit write failed (%s)", exc)


REMOVAL_JUDGE_SYSTEM_PROMPT = (
    "You guard the finish line of an autonomous agent's goal. The agent proposes removing or "
    "editing a criterion or a shell gate. Approve ONLY if the change keeps the finish line honest: "
    "the item is redundant, wrong, impossible, or out of scope, and the goal's evidence of done is "
    "not weakened just so the agent can finish sooner. When in doubt, reject.\n"
    "Reply with one line of JSON only, no prose:\n"
    '{"approved": true|false, "reason": "<short>"}'
)


def _judge_removal(session_id: str, goal_mgr: Any, change: str, reason: str) -> tuple[bool, str, str]:
    """Ask the goal judge whether *change* is honest. Any failure rejects (fail closed)."""
    del session_id
    state = goal_mgr.state
    lines = [f"Goal: {state.goal}"]
    if state.has_contract():
        lines += ["", state.contract.render_block()]
    lines += ["", "Criteria:"]
    lines += [f"  {i}. {text}" for i, text in enumerate(state.subgoals, 1)] or ["  (none)"]
    lines += ["", "Gates:"]
    lines += [f"  {i}. $ {gate.command}" for i, gate in enumerate(state.gates, 1)] or ["  (none)"]
    lines += ["", f"Proposed change: {change}", f"Agent's reason: {reason}"]
    evidence = [f"{label}: {value}" for label, value in (
        ("Last judge verdict", state.last_verdict), ("Last judge reason", state.last_reason)) if value]
    if evidence:
        lines += ["", "Recent evidence:"] + evidence
    try:
        from agent.auxiliary_client import call_llm
        from hermes_cli.goals import _call_goal_judge_llm, _goal_judge_timeout

        raw = _call_goal_judge_llm(call_llm, REMOVAL_JUDGE_SYSTEM_PROMPT, "\n".join(lines), _goal_judge_timeout())
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            newline = text.find("\n")
            if newline != -1:
                text = text[newline + 1:]
        if not text:
            raise ValueError("empty judge reply")
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("approved"), bool):
            raise ValueError(f"bad judge reply: {text[:200]}")
        return data["approved"], str(data.get("reason") or ""), ""
    except Exception as exc:
        logger.info("goal-full: removal judge failed (%s)", exc)
        return False, "judge unavailable", str(exc) or type(exc).__name__


_UNJUDGED_PATH = "the user's own /subgoal remove N / /goal gate remove N is the unjudged path."


def _judged_change(session_id: str, mgr: Any, action: str, args: dict) -> dict[str, Any]:
    """remove_criterion / remove_gate / edit_criterion: validate, cap, judge, then apply or refuse."""
    if not mgr.is_active():
        return {"error": "no active goal — start one with /goalfull <task>"}
    state = mgr.state
    raw_index = args.get("index")
    reason = str(args.get("reason") or "").strip()
    if action == "remove_criterion" and raw_index is None and args.get("text"):
        return {"error": "index and reason are required for remove_criterion",
                "user_command": "ask the user to run /subgoal remove N (the unjudged path)"}
    if isinstance(raw_index, bool):
        raw_index = None
    if isinstance(raw_index, str) and raw_index.strip().isdigit():
        raw_index = int(raw_index.strip())
    is_gate = action == "remove_gate"
    items = [g.command for g in state.gates] if is_gate else list(state.subgoals)
    if not isinstance(raw_index, int) or not 1 <= raw_index <= len(items):
        return {"error": f"index must be an integer from 1 to {len(items)}"}
    if not reason:
        return {"error": "reason is required"}
    index = raw_index
    new_text = ""
    if action == "edit_criterion":
        new_text = str(args.get("new_text") or "").strip()
        if not new_text:
            return {"error": "new_text is required"}

    is_edit = action == "edit_criterion"
    counter = "edits_used" if is_edit else "removals_used"
    cap = _load_goalfull_cfg()["max_edits_per_goal" if is_edit else "max_removals_per_goal"]
    data = _load_sidecar(session_id, state.created_at)
    if data[counter] >= cap:
        kind = "edit" if is_edit else "removal"
        return {"error": f"{kind} cap reached ({cap} per goal); ask the user to run /subgoal remove N "
                         "or /goal gate remove N"}

    old = items[index - 1]
    if is_edit:
        change = f"replace criterion {index}: {old} -> {new_text}"
    else:
        change = f"remove {'gate' if is_gate else 'criterion'} {index}: {old}"

    approved, judge_reason, err = _judge_removal(session_id, mgr, change, reason)
    row = {"session": session_id, "action": action, "change": change, "reason": reason,
           "approved": approved, "judge_reason": judge_reason}
    if not approved:
        _append_audit({**row, "error": err})
        return {"ok": False, "rejected": True, "reason": judge_reason,
                "error": f"judge rejected — {judge_reason}; {_UNJUDGED_PATH}"}

    if is_gate:
        mgr.remove_gate(index)
    elif is_edit:
        criteria = list(state.subgoals)
        criteria[index - 1] = new_text
        mgr.clear_subgoals()
        for text in criteria:
            mgr.add_subgoal(text)
    else:
        mgr.remove_subgoal(index)
    data[counter] += 1
    _save_sidecar(session_id, data)
    _append_audit(row)

    result: dict[str, Any] = {"ok": True, "action": action, "updated" if is_edit else "removed": old}
    if is_gate:
        result["gates"] = [g.command for g in mgr.state.gates]
    else:
        result["criteria"] = list(mgr.state.subgoals)
    result["judge"] = judge_reason
    return result


def _handle_start(
    session_id: str,
    objective: str,
    max_turns: Any = None,
    plan_call: Optional[Callable[[str], str]] = None,
    draft_contract_call: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Start a goal the agent chose to set: drafted, stored, no approval. Never replaces one."""
    objective = (objective or "").strip()
    if not objective:
        return {"error": "objective is required"}

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id=session_id)
    if mgr.is_active():
        return {
            "error": "a goal is already active; extend it with add_criterion/add_gate or show it",
            "refused": True, "goal": mgr.state.goal, "max_turns": mgr.state.max_turns,
        }

    contract = None
    try:
        if draft_contract_call is not None:
            contract = draft_contract_call(objective)
        else:
            from hermes_cli import goals
            contract = goals.draft_contract(objective)
    except Exception as exc:
        logger.info("goal-full: contract draft failed (%s)", exc)

    subgoals: list[str] = []
    gates: list[str] = []
    try:
        subgoals, gates = _parse_plan(plan_call(objective) if plan_call else _default_plan_call(objective))
    except Exception as exc:
        logger.info("goal-full: plan draft failed (%s)", exc)
    subgoals, _ = _clamp_subgoals(subgoals)

    budget = 0
    try:
        budget = int(max_turns) if max_turns else 0
    except (TypeError, ValueError):
        pass
    if budget <= 0:
        budget = _load_goalfull_cfg()["agent_started_max_turns"]
    if budget <= 0:
        budget = _GOALFULL_DEFAULTS["agent_started_max_turns"]

    mgr.set(objective, max_turns=budget, contract=contract)
    stored = [mgr.add_subgoal(text) for text in subgoals]
    gate_commands = [mgr.add_gate(command).command for command in gates]
    _reset_sidecar(session_id, mgr.state.created_at)

    return {
        "ok": True, "started": True, "goal": objective, "max_turns": budget,
        "subgoals": len(stored), "gates": len(gate_commands), "contract": contract is not None,
    }


def goal_update(args: dict, session_id: str = "", **kwargs: Any) -> str:
    """Start a goal, add a criterion or a gate to the standing goal, or read it back. Never raises."""
    del kwargs
    try:
        return json.dumps(_goal_update(args or {}, session_id))
    except Exception as exc:
        logger.info("goal-full: goal_update failed (%s)", exc)
        return json.dumps({"error": str(exc)})


def _goal_update(args: dict, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {"error": "no session attached to this tool call, so there is no goal to update"}

    action = str(args.get("action") or "").strip()
    if action == "start":
        return _handle_start(session_id, str(args.get("objective") or ""), args.get("max_turns"))

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id=session_id)
    if not mgr.has_goal():
        return {"error": "no active goal — start one with /goalfull <task>"}
    state = mgr.state

    if action == "show":
        return {"ok": True, "goal": _goal_view(mgr)}

    if action == "add_criterion":
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "text is required"}
        if text in state.subgoals:
            return {"ok": True, "duplicate": True, "criteria": list(state.subgoals)}
        if len(state.subgoals) >= _MAX_CRITERIA:
            return {"error": "ten criteria is the limit; ask the user to remove one with /subgoal remove N"}
        added = mgr.add_subgoal(text)
        return {"ok": True, "added": added, "criteria": list(mgr.state.subgoals)}

    if action == "add_gate":
        command = str(args.get("command") or "").strip()
        if not command:
            return {"error": "command is required"}
        if len(state.gates) >= _MAX_GATES:
            return {"error": "six gates is the limit; ask the user to remove one with /goal gate remove N"}
        timeout = min(int(args.get("timeout_seconds") or _MAX_GATE_TIMEOUT), _MAX_GATE_TIMEOUT)
        gate = mgr.add_gate(command, timeout_seconds=timeout, max_retries=1)
        return {
            "ok": True, "gate": gate.command, "gates": [g.command for g in mgr.state.gates],
            "note": "the gate runs before the next judge call; the goal cannot be done until it exits 0",
        }

    if action in ("remove_criterion", "remove_gate", "edit_criterion"):
        return _judged_change(session_id, mgr, action, args)

    return {
        "error": f"{action!r} is not something I can do to your own goal.",
        "user_command": {"remove_criterion": "/subgoal remove <N>",
                         "remove_gate": "/goal gate remove <N>",
                         "set_contract": "/goalfull <task>"}.get(action, "/goal show"),
        "note": "Criteria and gates can only be added from inside a turn; removing or relaxing one is the user's call.",
    }


_FINISH_LINE_TAIL = "Your work is judged against this finish line; do not stop early; report evidence for every criterion."


def _finish_line_block(state: Any) -> str:
    lines = [f"GOAL FINISH LINE: {state.goal}"]
    lines += [f"{i}. {text}" for i, text in enumerate(state.subgoals, 1)]
    lines += [f"$ {gate.command}" for gate in state.gates]
    lines.append(_FINISH_LINE_TAIL)
    return "\n".join(lines)


def _with_finish_line(task: dict, block: str) -> dict:
    out = dict(task)
    out["goal"] = block + "\n\n" + str(task.get("goal") or "")
    context = task.get("context")
    if isinstance(context, str) and context.strip():
        out["context"] = context + "\n\n" + block
    return out


def _pre_tool_call_finish_line(tool_name: str = "", args: Any = None, session_id: str = "", **kwargs: Any):
    """pre_tool_call: prepend the active goal's finish line to every delegate_task brief. Fails open."""
    try:
        if tool_name != "delegate_task" or not session_id or not isinstance(args, dict):
            return None
        if str(args.get("action") or "spawn") == "continue":
            return None
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=session_id)
        if not mgr.is_active():
            return None
        block = _finish_line_block(mgr.state)
        tasks = args.get("tasks")
        if isinstance(tasks, list) and tasks:
            new = [_with_finish_line(t, block) if isinstance(t, dict) else t for t in tasks]
            return {"action": "modify", "args": {"tasks": new}}
        if isinstance(args.get("goal"), str) and args["goal"].strip():
            single = _with_finish_line({"goal": args["goal"], "context": args.get("context")}, block)
            if single.get("context") == args.get("context"):
                single.pop("context")
            return {"action": "modify", "args": single}
        return None
    except Exception as exc:
        logger.info("goal-full: pre_tool_call hook failed open (%s)", exc)
        return None


_MAX_DELEGATION_SUMMARY = 8000


def _delegation_summaries(result: Any) -> Optional[str]:
    """Joined summaries of a finished blocking delegate_task result; None for acks/running/failed-only."""
    payload = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") == "dispatched" or payload.get("mode") == "background":
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    entries = [e for e in results if isinstance(e, dict) and e.get("status") == "completed"
               and str(e.get("summary") or "").strip()]
    if not entries:
        return None
    return "\n\n".join(str(e["summary"]) for e in entries)[:_MAX_DELEGATION_SUMMARY]


def _transform_delegation_result(tool_name: str = "", args: Any = None, result: Any = None,
                                 session_id: str = "", **kwargs: Any):
    """transform_tool_result: judge a finished delegation against the parent's goal. Fails open."""
    try:
        if tool_name != "delegate_task" or not session_id:
            return None
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=session_id)
        if not mgr.is_active():
            return None
        summaries = _delegation_summaries(result)
        if summaries is None:
            return None
        args = args if isinstance(args, dict) else {}
        state = mgr.state
        data = _load_sidecar(session_id, state.created_at)
        if str(args.get("action") or "spawn") == "spawn":
            data["delegation_retries"] = 0
            _save_sidecar(session_id, data)
        from hermes_cli.goals import judge_goal

        verdict, reason, parse_failed, _wait, transport_failed = judge_goal(
            state.goal, summaries, subgoals=list(state.subgoals),
            contract=state.contract if state.has_contract() else None)
        if parse_failed or transport_failed:
            return None
        base = result if isinstance(result, str) else json.dumps(result)
        row = {"session": session_id, "action": "delegation_judge", "approved": verdict == "done",
               "judge_reason": reason}
        if verdict == "done":
            _append_audit({**row, "retries": data["delegation_retries"]})
            return base + "\n\n✓ goal judge: accepted"
        cap = _load_goalfull_cfg()["delegation_judge_retries"]
        if data["delegation_retries"] >= cap:
            _append_audit({**row, "retries": data["delegation_retries"]})
            return (base + "\n\n⚠ goal judge kept rejecting the delegation (" + str(reason)
                    + "); check /goal show and the audit file.")
        data["delegation_retries"] += 1
        _save_sidecar(session_id, data)
        _append_audit({**row, "retries": data["delegation_retries"]})
        return (f"GOAL JUDGE: delegation NOT accepted — {reason}. Re-dispatch with action='continue' "
                "and the same task goal so the child keeps its transcript.")
    except Exception as exc:
        logger.info("goal-full: transform_tool_result hook failed open (%s)", exc)
        return None


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_command(
        name=COMMAND,
        handler=handle,
        description=(
            "Draft a complete goal from one sentence, show it, and start the loop only after "
            "you approve it. Usage: /goalfull <what should be true when this is done>"
        ),
        args_hint="<task>",
    )
    if getattr(ctx, "register_tool", None) is not None:
        ctx.register_tool(
            name=TOOL_NAME, toolset="goal", schema=_GOAL_UPDATE_SCHEMA, handler=goal_update,
            description="Start a goal for a big task, add a criterion or a gate to it, or read it back.",
            emoji="🎯",
        )
    if getattr(ctx, "register_hook", None) is not None:
        ctx.register_hook("pre_tool_call", _pre_tool_call_finish_line)
    if getattr(ctx, "register_hook", None) is not None:
        ctx.register_hook("transform_tool_result", _transform_delegation_result)


__all__ = ["COMMAND", "TOOL_NAME", "apply_plan", "goal_update", "handle", "register", "_parse_plan"]
