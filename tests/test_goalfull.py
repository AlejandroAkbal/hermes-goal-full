"""The `/goalfull` command sets the contract, the subgoals, and the gates in one call.

Runs the plugin's handler against Hermes's real goal storage with both model calls
replaced by fixed replies, then reads back the row Hermes persists and checks all
three landed. No network, no running gateway.

Goal storage resolves its database from HERMES_HOME and caches the handle, so this
sets a throwaway home before importing anything from hermes_cli and never touches
the real session database.

Run with an interpreter that can import Hermes: either install Hermes into the
environment, or put its checkout on PYTHONPATH:

  PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import types
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"

# Before any hermes_cli import: goal storage caches its database per HERMES_HOME.
_HOME = tempfile.TemporaryDirectory()
os.environ["HERMES_HOME"] = _HOME.name


def _load_plugin():
    name = "goal_full_plugin_under_test"
    spec = importlib.util.spec_from_file_location(name, _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GoalfullCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    @classmethod
    def tearDownClass(cls):
        _HOME.cleanup()

    def _plan(self, _task: str) -> str:
        return json.dumps({
            "subgoals": [
                "find the extension point that registers a slash command",
                "confirm a plugin can call the goal API",
                "register the command",
                "run a test that checks the stored goal",
            ],
            "gates": [{"command": "python3 -m unittest tests.test_goalfull -v"}],
        })

    def test_one_command_sets_contract_subgoals_and_gates(self):
        from hermes_cli import goals as goals_module
        from hermes_cli.goals import GoalContract, GoalManager

        task = "make the memory UI show Honcho as enabled, with the conclusion count"
        contract = GoalContract(
            outcome="the Honcho card shows enabled and the conclusion count",
            verification="the honcho endpoint returns enabled true and a non-zero conclusion count",
            constraints="do not edit Hermes source",
            boundaries="the memory UI plugin only",
            stop_when="the endpoint needs credentials or is unreachable",
        )
        injected: list[tuple[str, str]] = []
        original = goals_module.draft_contract
        goals_module.draft_contract = lambda objective, timeout=None: contract
        try:
            reply = self.plugin.apply_plan(
                "session-under-test", task, plan_call=self._plan,
                inject=lambda text, key: injected.append((text, key)) or True,
                approve=lambda question, choices: "approve",
                session_key="agent:main:test",
            )
        finally:
            goals_module.draft_contract = original

        stored = GoalManager(session_id="session-under-test")
        self.assertTrue(stored.has_goal(), reply)
        self.assertTrue(stored.has_contract(), reply)
        self.assertEqual(stored.state.contract.outcome, contract.outcome)
        self.assertEqual(stored.state.contract.verification, contract.verification)
        self.assertEqual(stored.state.contract.boundaries, contract.boundaries)
        self.assertEqual(len(stored.state.subgoals), 4, stored.state.subgoals)
        self.assertEqual(len(stored.state.gates), 1, stored.state.gates)
        self.assertIn("unittest", stored.state.gates[0].command)

        # The same session read back from storage, not from the object the command held.
        reloaded = GoalManager(session_id="session-under-test")
        reloaded.refresh()
        self.assertEqual(len(reloaded.state.subgoals), 4)
        self.assertEqual(len(reloaded.state.gates), 1)

        self.assertTrue(injected, "the command did not queue the kickoff turn")
        self.assertIn(stored.state.goal, injected[0][0])
        self.assertEqual(injected[0][1], "agent:main:test")

    def test_rejection_stores_nothing(self):
        from hermes_cli import goals as goals_module
        from hermes_cli.goals import GoalContract, GoalManager

        contract = GoalContract(outcome="done", verification="the test exits 0")
        shown: list[str] = []

        def reject(question, choices):
            shown.append(question)
            return "reject"

        original = goals_module.draft_contract
        goals_module.draft_contract = lambda objective, timeout=None: contract
        try:
            reply = self.plugin.apply_plan(
                "session-rejected", "do the thing", plan_call=self._plan, approve=reject,
            )
        finally:
            goals_module.draft_contract = original

        self.assertIn("Nothing stored", reply)
        self.assertTrue(shown, "the draft was never shown for approval")
        self.assertIn("do the thing", shown[0])
        self.assertFalse(GoalManager(session_id="session-rejected").has_goal())

    def test_empty_task_sets_nothing(self):
        from hermes_cli.goals import GoalManager

        reply = self.plugin.apply_plan("session-empty", "   ", plan_call=self._plan)
        self.assertIn("Usage", reply)
        self.assertFalse(GoalManager(session_id="session-empty").has_goal())

    def test_garbage_plan_keeps_the_contract(self):
        from hermes_cli import goals as goals_module
        from hermes_cli.goals import GoalContract, GoalManager

        contract = GoalContract(outcome="done", verification="the test exits 0")
        original = goals_module.draft_contract
        goals_module.draft_contract = lambda objective, timeout=None: contract
        try:
            self.plugin.apply_plan(
                "session-garbage", "do the thing", plan_call=lambda task: "not json at all",
                approve=lambda question, choices: "approve",
            )
        finally:
            goals_module.draft_contract = original

        stored = GoalManager(session_id="session-garbage")
        self.assertTrue(stored.has_contract())
        self.assertEqual(stored.state.subgoals, [])
        self.assertEqual(stored.state.gates, [])

    def _handle_with_context(self, ctx, task: str, *, session_id: str, session_key: str = "agent:main:desktop"):
        """Register *ctx*, run the command the way Hermes does, then forget the context."""
        original_plan, original_ctx = self.plugin._default_plan_call, self.plugin._CTX
        self.plugin._default_plan_call = self._plan
        try:
            self.plugin.register(ctx)
            return ctx.commands[self.plugin.COMMAND](task, session_id=session_id, session_key=session_key)
        finally:
            self.plugin._default_plan_call = original_plan
            self.plugin._CTX = original_ctx

    def test_handle_stores_the_approved_draft_once(self):
        from hermes_cli import goals as goals_module
        from hermes_cli.goals import GoalContract, GoalManager

        contract = GoalContract(outcome="done", verification="the test exits 0")
        drafts: list[str] = []

        def draft(objective, timeout=None):
            drafts.append(objective)
            return contract

        ctx = _FakeContext(answer="approve")
        original = goals_module.draft_contract
        goals_module.draft_contract = draft
        try:
            reply = self._handle_with_context(ctx, "ship the memory UI", session_id="session-handle")
        finally:
            goals_module.draft_contract = original

        stored = GoalManager(session_id="session-handle")
        self.assertTrue(stored.has_goal(), reply)
        self.assertEqual(stored.state.contract.verification, "the test exits 0")
        self.assertEqual(len(stored.state.subgoals), 4)
        self.assertEqual(len(stored.state.gates), 1)
        self.assertEqual(len(drafts), 1, "the approved contract was drafted a second time")
        self.assertEqual(len(ctx.asked), 1, "the draft was never shown for approval")
        self.assertEqual(len(ctx.injected), 1, "the kickoff turn was not queued")
        self.assertEqual(ctx.injected[0][1], "agent:main:desktop")
        self.assertIn("Subgoals:", reply)

    def test_handle_rejection_stores_nothing(self):
        from hermes_cli.goals import GoalManager

        ctx = _FakeContext(answer="reject")
        reply = self._handle_with_context(ctx, "ship the memory UI", session_id="session-handle-reject")

        self.assertIn("Nothing stored", reply)
        self.assertFalse(GoalManager(session_id="session-handle-reject").has_goal())
        self.assertEqual(ctx.injected, [])

    def test_handle_without_a_context_stores_nothing(self):
        from hermes_cli.goals import GoalManager

        original = self.plugin._CTX
        self.plugin._CTX = None
        try:
            reply = self.plugin.handle("ship the memory UI", session_id="session-no-context")
        finally:
            self.plugin._CTX = original

        self.assertIn("approval prompt is not reachable", reply)
        self.assertFalse(GoalManager(session_id="session-no-context").has_goal())

    def test_handle_without_arguments_shows_usage(self):
        from hermes_cli.goals import GoalManager

        ctx = _FakeContext()
        reply = self._handle_with_context(ctx, "   ", session_id="session-handle-empty")

        self.assertIn("Usage", reply)
        self.assertFalse(GoalManager(session_id="session-handle-empty").has_goal())
        self.assertEqual(ctx.asked, [])

    def _apply_with_contract(self, session_id: str, task: str, **kwargs):
        from hermes_cli import goals as goals_module
        from hermes_cli.goals import GoalContract

        original = goals_module.draft_contract
        goals_module.draft_contract = lambda objective, timeout=None: GoalContract(
            outcome="done", verification="the test exits 0",
        )
        try:
            return self.plugin.apply_plan(session_id, task, **kwargs)
        finally:
            goals_module.draft_contract = original

    def test_short_plan_is_shown_with_a_note_not_retried(self):
        from hermes_cli.goals import GoalManager

        calls: list[str] = []
        asked: list[str] = []

        def plan(task):
            calls.append(task)
            return json.dumps({"subgoals": ["the only step"], "gates": []})

        self._apply_with_contract(
            "session-short", "do the short thing", plan_call=plan,
            approve=lambda question, choices: asked.append(question) or "approve",
        )
        self.assertEqual(len(calls), 1, "the model was retried for a short plan")
        self.assertIn("subgoal(s); goal-full expects", asked[0])
        self.assertEqual(len(GoalManager(session_id="session-short").state.subgoals), 1)

    def test_long_plan_keeps_the_first_six(self):
        from hermes_cli.goals import GoalManager

        steps = [f"step number {i} of the plan" for i in range(1, 10)]
        self._apply_with_contract(
            "session-long", "do the long thing",
            plan_call=lambda task: json.dumps({"subgoals": steps, "gates": []}),
            approve=lambda question, choices: "approve",
        )
        stored = GoalManager(session_id="session-long").state
        self.assertEqual(len(stored.subgoals), 6)
        self.assertIn(steps[5], repr(stored))
        self.assertNotIn(steps[6], repr(stored))

    def test_approval_question_stays_short_and_the_plan_is_posted_first(self):
        task = "ship the memory UI " + "with every card rendering its live status " * 5
        ctx = _FakeContext(answer="reject")
        self._handle_with_context(ctx, task, session_id="session-posted")

        question = ctx.asked[0][0]
        self.assertLessEqual(len(question), 200, question)
        self.assertEqual(len(ctx.posted), 1)
        plan = json.loads(self._plan(task))
        self.assertIn("Subgoals:", ctx.posted[0])
        for step in plan["subgoals"]:
            self.assertIn(step, ctx.posted[0])
        self.assertIn(plan["gates"][0]["command"], ctx.posted[0])
        self.assertNotIn(plan["subgoals"][0], question)

    def test_question_falls_back_to_the_plan_when_posting_fails(self):
        class _BrokenPost(_FakeContext):
            def post_message(self, text):
                raise RuntimeError("no session bound")

        ctx = _BrokenPost(answer="reject")
        self._handle_with_context(ctx, "ship the memory UI", session_id="session-post-fails")
        self.assertIn("Subgoals:", ctx.asked[0][0])
        self.assertIn("find the extension point", ctx.asked[0][0])

        asked: list[str] = []
        self._apply_with_contract(
            "session-notify-false", "do the thing", plan_call=self._plan, notify=lambda text: False,
            approve=lambda question, choices: asked.append(question) or "reject",
        )
        self.assertIn("Subgoals:", asked[0])

    def test_clamp_and_summary_are_pure(self):
        self.assertEqual(self.plugin._clamp_subgoals(list("abcdefgh")), (list("abcdef"), ""))
        kept, note = self.plugin._clamp_subgoals(["a", "b"])
        self.assertEqual(kept, ["a", "b"])
        self.assertIn("2 subgoal(s); goal-full expects 3-6", note)
        question = self.plugin._summary_question("x" * 300, True, 4, 1)
        self.assertLess(len(question), 160)
        self.assertIn("contract 5/5 fields, 4 subgoal(s), 1 gate(s)", question)

    def _seed(self, sid, criteria=("step one",), gates=()):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id=sid)
        mgr.set("ship the thing", max_turns=20)
        for text in criteria:
            mgr.add_subgoal(text)
        for command in gates:
            mgr.add_gate(command)
        return mgr

    def _update(self, sid, **args):
        return json.loads(self.plugin.goal_update(args, session_id=sid))

    def test_goal_update_registers_with_goal_toolset(self):
        ctx = _FakeContext()
        original = self.plugin._CTX
        try:
            self.plugin.register(ctx)
        finally:
            self.plugin._CTX = original
        self.assertIn("goal_update", ctx.tools)
        self.assertEqual(ctx.tools["goal_update"]["toolset"], "goal")

    def test_show_returns_contract_criteria_and_gates(self):
        self._seed("tool-show", criteria=("step one", "step two"), gates=("pytest -q",))
        reply = self.plugin.goal_update({"action": "show"}, session_id="tool-show")
        self.assertIn("ship the thing", reply)
        self.assertIn("step one", reply)
        self.assertIn("step two", reply)
        self.assertIn("pytest -q", reply)

    def test_show_without_a_goal_is_an_error(self):
        self.assertIn("error", self._update("tool-no-goal", action="show"))
        self.assertIn("error", self._update("", action="show"))

    def test_add_criterion_reaches_the_judge(self):
        from hermes_cli.goals import _render_extra_criteria, load_goal

        self._seed("tool-criterion")
        reply = self._update("tool-criterion", action="add_criterion", text="  help mentions --dry-run ")
        self.assertTrue(reply["ok"], reply)
        state = load_goal("tool-criterion")
        self.assertEqual(state.subgoals, ["step one", "help mentions --dry-run"])
        self.assertIn("Extra criterion 2: help mentions --dry-run", _render_extra_criteria(state.subgoals))

    def test_add_criterion_is_idempotent(self):
        from hermes_cli.goals import load_goal

        self._seed("tool-dup")
        self._update("tool-dup", action="add_criterion", text="the same")
        reply = self._update("tool-dup", action="add_criterion", text="the same")
        self.assertTrue(reply["duplicate"], reply)
        self.assertEqual(load_goal("tool-dup").subgoals.count("the same"), 1)

    def test_add_criterion_stops_at_ten(self):
        from hermes_cli.goals import load_goal

        self._seed("tool-ten", criteria=[f"criterion {i}" for i in range(10)])
        reply = self._update("tool-ten", action="add_criterion", text="the eleventh")
        self.assertIn("error", reply)
        self.assertEqual(len(load_goal("tool-ten").subgoals), 10)

    def test_add_gate_clamps_timeout_and_retries(self):
        from hermes_cli.goals import load_goal

        self._seed("tool-gate")
        reply = self._update("tool-gate", action="add_gate", command="pytest -q", timeout_seconds=900)
        self.assertTrue(reply["ok"], reply)
        gate = load_goal("tool-gate").gates[0]
        self.assertEqual(gate.command, "pytest -q")
        self.assertLessEqual(gate.timeout_seconds, 120)
        self.assertEqual(gate.max_retries, 1)

    def test_add_gate_stops_at_six(self):
        from hermes_cli.goals import load_goal

        self._seed("tool-six", gates=[f"true {i}" for i in range(6)])
        reply = self._update("tool-six", action="add_gate", command="true 7")
        self.assertIn("error", reply)
        self.assertEqual(len(load_goal("tool-six").gates), 6)

    def test_removal_returns_the_user_command(self):
        from hermes_cli.goals import load_goal

        self._seed("tool-remove", criteria=("keep me",))
        reply = self._update("tool-remove", action="remove_criterion", text="keep me")
        self.assertIn("error", reply)
        self.assertIn("/subgoal remove", reply["user_command"])
        self.assertEqual(load_goal("tool-remove").subgoals, ["keep me"])

    def test_goal_update_description_names_subgoal_remove(self):
        self.assertIn("/subgoal remove", self.plugin._GOAL_UPDATE_SCHEMA["description"])

    def test_add_criterion_preserves_the_rest_of_the_goal(self):
        from hermes_cli.goals import GoalContract, GoalManager, load_goal

        mgr = GoalManager(session_id="tool-preserve")
        mgr.set("ship the thing", max_turns=7, contract=GoalContract(outcome="shipped", verification="pytest -q exits 0"))
        mgr.state.turns_used = 3
        mgr._save()
        mgr.add_gate("pytest -q")

        reply = self._update("tool-preserve", action="add_criterion", text="help mentions --dry-run")
        self.assertTrue(reply["ok"], reply)
        state = load_goal("tool-preserve")
        self.assertEqual(state.status, "active")
        self.assertEqual(state.max_turns, 7)
        self.assertEqual(state.turns_used, 3)
        self.assertEqual(state.contract.outcome, "shipped")
        self.assertEqual([g.command for g in state.gates], ["pytest -q"])

    def test_kickoff_seeds_the_todo_list(self):
        injected: list[str] = []
        self._apply_with_contract(
            "session-seed", "do the seeded thing", plan_call=self._plan,
            inject=lambda text, key: injected.append(text) or True,
            approve=lambda question, choices: "approve",
        )
        self.assertIn("todo_list", injected[0])
        self.assertIn("1. find the extension point that registers a slash command", injected[0])

    def test_kickoff_seeds_even_when_the_goal_was_just_typed(self):
        from hermes_cli.goals import GOAL_ALREADY_SEEN_KICK

        task = "ship the memory UI " + "with every card rendering its live status " * 12
        self.assertGreater(len(task), 400)
        injected: list[str] = []
        self._apply_with_contract(
            "session-seed-seen", task, plan_call=self._plan,
            inject=lambda text, key: injected.append(text) or True,
            approve=lambda question, choices: "approve",
        )
        self.assertTrue(injected[0].startswith(GOAL_ALREADY_SEEN_KICK), injected[0])
        self.assertIn("todo_list", injected[0])
        self.assertIn("1. find the extension point that registers a slash command", injected[0])


class _FakeContext:
    """The PluginContext surface the command uses, recorded instead of prompted."""

    def __init__(self, answer: str = "approve"):
        self.answer = answer
        self.commands: dict[str, Any] = {}
        self.asked: list[tuple[str, list]] = []
        self.injected: list[tuple[str, str]] = []
        self.posted: list[str] = []
        self.tools: dict[str, Any] = {}

    def register_command(self, *, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_tool(self, *, name, toolset, schema, handler, description="", emoji="", **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema,
                            "handler": handler, "description": description}

    def ask_choice(self, question, choices):
        self.asked.append((question, choices))
        return self.answer

    def post_message(self, text):
        self.posted.append(text)
        return types.SimpleNamespace(ok=True, edit=lambda *args, **kwargs: None)

    def inject_message(self, content, role="user", **kwargs):
        self.injected.append((content, kwargs.get("session_key", "")))
        return True


if __name__ == "__main__":
    unittest.main()
