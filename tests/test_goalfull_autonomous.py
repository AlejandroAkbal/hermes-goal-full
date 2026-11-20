"""Agent-started goals: `goal_update start`, the guard against replacing a goal, and config.

Model calls are replaced by patches (plan) and fixed replies (contract); no network.
HERMES_HOME always points at a fresh temp dir, set before any hermes_cli import: the
shell may export the real ~/.hermes, and test_goalfull deletes its own temp home in
tearDownClass, so inheriting either would be wrong. The dir is kept, never deleted.

Run with the same interpreter and PYTHONPATH as tests/test_goalfull.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="goalfull-autonomous-")


def _load_plugin():
    name = "goal_full_autonomous_under_test"
    spec = importlib.util.spec_from_file_location(name, _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(_objective: str) -> str:
    return json.dumps({
        "subgoals": ["write the code", "write the tests", "run the suite"],
        "gates": [{"command": "pytest -q"}],
    })


def _contract():
    from hermes_cli.goals import GoalContract

    return GoalContract(
        outcome="it ships", verification="the suite passes", constraints="none",
        boundaries="this repo", stop_when="blocked",
    )


class GoalfullAutonomousTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def _seed(self, sid):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=sid)
        mgr.set("the original goal", max_turns=20)
        for text in ("one", "two", "three"):
            mgr.add_subgoal(text)
        mgr.add_gate("true")
        return mgr

    def _start(self, sid, *, plan=_plan, contract=None, **args):
        from hermes_cli import goals as goals_module

        with mock.patch.object(self.plugin, "_default_plan_call", side_effect=plan), \
                mock.patch.object(goals_module, "draft_contract", return_value=contract):
            return json.loads(self.plugin.goal_update({"action": "start", **args}, session_id=sid))

    def test_start_creates_goal(self):
        from hermes_cli.goals import GoalManager

        reply = self._start("auto-start", contract=_contract(), objective="ship it")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["max_turns"], 10)
        self.assertTrue(reply["contract"])
        state = GoalManager(session_id="auto-start").state
        self.assertEqual(state.goal, "ship it")
        self.assertEqual(state.status, "active")
        self.assertEqual(len(state.subgoals), 3)
        self.assertEqual(len(state.gates), 1)
        self.assertEqual(state.max_turns, 10)
        self.assertTrue(state.has_contract())

    def test_start_refuses_when_active(self):
        from hermes_cli.goals import GoalManager

        self._seed("auto-refuse")
        reply = self._start("auto-refuse", objective="a different goal")
        self.assertIn("already active", reply["error"])
        self.assertTrue(reply["refused"])
        state = GoalManager(session_id="auto-refuse").state
        self.assertEqual(state.goal, "the original goal")
        self.assertEqual(len(state.subgoals), 3)

    def test_start_empty_objective(self):
        from hermes_cli.goals import GoalManager

        self.assertIn("error", self._start("auto-empty", objective="   "))
        self.assertFalse(GoalManager(session_id="auto-empty").has_goal())

    def test_start_plan_call_fallback(self):
        from hermes_cli.goals import GoalManager

        def broken(_objective):
            raise RuntimeError("aux down")

        reply = self._start("auto-fallback", plan=broken, objective="ship it anyway")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["max_turns"], 10)
        state = GoalManager(session_id="auto-fallback").state
        self.assertEqual(state.goal, "ship it anyway")
        self.assertEqual(state.subgoals, [])
        self.assertEqual(state.max_turns, 10)

    def test_start_respects_max_turns(self):
        from hermes_cli.goals import GoalManager

        reply = self._start("auto-budget", objective="short one", max_turns=4)
        self.assertEqual(reply["max_turns"], 4)
        self.assertEqual(GoalManager(session_id="auto-budget").state.max_turns, 4)

    def test_other_actions_still_need_a_goal(self):
        reply = json.loads(self.plugin.goal_update({"action": "show"}, session_id="auto-none"))
        self.assertIn("no active goal", reply["error"])

    def test_config_defaults(self):
        cfg = self.plugin._load_goalfull_cfg({})
        self.assertEqual(cfg, {
            "agent_started_max_turns": 10, "require_approval": False,
            "max_removals_per_goal": 3, "max_edits_per_goal": 3, "delegation_judge_retries": 2,
        })

    def test_config_override(self):
        cfg = self.plugin._load_goalfull_cfg(
            {"goalfull": {"agent_started_max_turns": 3, "max_removals_per_goal": 1}})
        self.assertEqual(cfg["agent_started_max_turns"], 3)
        self.assertEqual(cfg["max_removals_per_goal"], 1)
        self.assertEqual(cfg["max_edits_per_goal"], 3)
        bad = self.plugin._load_goalfull_cfg({"goalfull": {"agent_started_max_turns": "lots"}})
        self.assertEqual(bad["agent_started_max_turns"], 10)

    def test_description_has_start_and_soul_instruction(self):
        schema = self.plugin._GOAL_UPDATE_SCHEMA
        self.assertIn("start", schema["parameters"]["properties"]["action"]["enum"])
        self.assertIn("START GOALS YOURSELF", schema["description"])
        self.assertIn("/subgoal remove N", schema["description"])


class GoalfullJudgedRemovalTest(unittest.TestCase):
    """Phase 2: remove_criterion / remove_gate / edit_criterion through a stubbed goal judge."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        from hermes_cli import goals as goals_module

        self.judge_calls = []
        self.judge_reply = '{"approved": true, "reason": "ok"}'

        def fake_judge(_call_llm, system_prompt, user_prompt, _timeout):
            self.judge_calls.append((system_prompt, user_prompt))
            if isinstance(self.judge_reply, Exception):
                raise self.judge_reply
            return self.judge_reply

        self._judge_patch = mock.patch.object(goals_module, "_call_goal_judge_llm", side_effect=fake_judge)
        self._judge_patch.start()

    def tearDown(self):
        self._judge_patch.stop()

    _seed = GoalfullAutonomousTest._seed

    def _update(self, sid, **args):
        return json.loads(self.plugin.goal_update(args, session_id=sid))

    def _audit(self, sid):
        path = self.plugin._goalfull_state_dir() / "audit.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if row.get("session") == sid]

    def _sidecar(self, sid):
        from hermes_cli.goals import GoalManager

        return self.plugin._load_sidecar(sid, GoalManager(session_id=sid).state.created_at)

    def test_judge_removal_approve(self):
        mgr = self._seed("judge-approve")
        approved, reason, err = self.plugin._judge_removal("judge-approve", mgr, "remove criterion 1: one", "dup")
        self.assertEqual((approved, reason, err), (True, "ok", ""))
        prompt = self.judge_calls[0][1]
        self.assertIn("the original goal", prompt)
        self.assertIn("remove criterion 1: one", prompt)
        self.assertIn("dup", prompt)
        self.assertIn("$ true", prompt)

    def test_judge_removal_reject(self):
        mgr = self._seed("judge-reject")
        self.judge_reply = '{"approved": false, "reason": "weak"}'
        self.assertEqual(self.plugin._judge_removal("judge-reject", mgr, "c", "r"), (False, "weak", ""))

    def test_judge_removal_error_defaults_reject(self):
        mgr = self._seed("judge-error")
        self.judge_reply = RuntimeError("aux down")
        approved, reason, err = self.plugin._judge_removal("judge-error", mgr, "c", "r")
        self.assertFalse(approved)
        self.assertEqual(reason, "judge unavailable")
        self.assertIn("aux down", err)
        for garbage in ("", "not json", '{"approved": "yes"}', "[true]"):
            self.judge_reply = garbage
            self.assertFalse(self.plugin._judge_removal("judge-error", mgr, "c", "r")[0], garbage)

    def test_remove_criterion_approved_removes(self):
        from hermes_cli.goals import GoalManager

        self._seed("rm-approved")
        reply = self._update("rm-approved", action="remove_criterion", index=2, reason="duplicate of one")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["removed"], "two")
        self.assertEqual(GoalManager(session_id="rm-approved").state.subgoals, ["one", "three"])
        rows = self._audit("rm-approved")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["approved"])
        self.assertEqual(self._sidecar("rm-approved")["removals_used"], 1)

    def test_remove_criterion_rejected_no_change(self):
        from hermes_cli.goals import GoalManager

        self._seed("rm-rejected")
        self.judge_reply = '{"approved": false, "reason": "weakens the finish line"}'
        reply = self._update("rm-rejected", action="remove_criterion", index=1, reason="I want to finish")
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["rejected"])
        self.assertIn("judge rejected", reply["error"])
        self.assertIn("/subgoal remove N", reply["error"])
        self.assertEqual(GoalManager(session_id="rm-rejected").state.subgoals, ["one", "two", "three"])
        rows = self._audit("rm-rejected")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["approved"])
        self.assertEqual(self._sidecar("rm-rejected")["removals_used"], 0)

    def test_remove_gate_approved(self):
        from hermes_cli.goals import GoalManager

        self._seed("gate-approved")
        reply = self._update("gate-approved", action="remove_gate", index="1", reason="no-op gate")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["removed"], "true")
        self.assertEqual(reply["gates"], [])
        self.assertEqual(GoalManager(session_id="gate-approved").state.gates, [])
        self.assertEqual(self._sidecar("gate-approved")["removals_used"], 1)

    def test_edit_criterion_approved(self):
        from hermes_cli.goals import GoalManager

        self._seed("edit-approved")
        reply = self._update("edit-approved", action="edit_criterion", index=2,
                             new_text="two, precisely", reason="clarify")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["updated"], "two")
        state = GoalManager(session_id="edit-approved").state
        self.assertEqual(state.subgoals, ["one", "two, precisely", "three"])
        self.assertEqual(state.goal, "the original goal")
        self.assertEqual(len(state.gates), 1)
        sidecar = self._sidecar("edit-approved")
        self.assertEqual(sidecar["edits_used"], 1)
        self.assertEqual(sidecar["removals_used"], 0)

    def test_removal_cap_reached(self):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="rm-cap")
        mgr.set("the original goal", max_turns=20)
        for text in ("one", "two"):
            mgr.add_subgoal(text)
        cfg = {**self.plugin._GOALFULL_DEFAULTS, "max_removals_per_goal": 1}
        with mock.patch.object(self.plugin, "_load_goalfull_cfg", return_value=cfg):
            first = self._update("rm-cap", action="remove_criterion", index=1, reason="dup")
            second = self._update("rm-cap", action="remove_criterion", index=1, reason="dup")
        self.assertTrue(first["ok"], first)
        self.assertIn("cap reached (1 per goal)", second["error"])
        self.assertEqual(len(self.judge_calls), 1)
        self.assertEqual(GoalManager(session_id="rm-cap").state.subgoals, ["two"])

    def test_removal_empty_reason(self):
        from hermes_cli.goals import GoalManager

        self._seed("rm-noreason")
        reply = self._update("rm-noreason", action="remove_criterion", index=1, reason="  ")
        self.assertIn("reason", reply["error"])
        self.assertEqual(self.judge_calls, [])
        self.assertEqual(GoalManager(session_id="rm-noreason").state.subgoals, ["one", "two", "three"])

    def test_judge_raising_rejects(self):
        from hermes_cli.goals import GoalManager

        self._seed("rm-raise")
        self.judge_reply = RuntimeError("timeout")
        reply = self._update("rm-raise", action="remove_gate", index=1, reason="flaky")
        self.assertTrue(reply["rejected"])
        self.assertEqual(len(GoalManager(session_id="rm-raise").state.gates), 1)
        rows = self._audit("rm-raise")
        self.assertFalse(rows[0]["approved"])
        self.assertIn("timeout", rows[0]["error"])


_DONE = json.dumps({"results": [{"task_index": 0, "status": "completed", "summary": "child did the thing",
                                  "duration_seconds": 3.2}], "total_duration_seconds": 3.4})


class GoalfullDelegationTest(unittest.TestCase):
    """Phase 3: finish line in delegate_task briefs + goal judge on delegation results."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    _seed = GoalfullAutonomousTest._seed
    _sidecar = GoalfullJudgedRemovalTest._sidecar

    def _judge(self, reply):
        from hermes_cli import goals as goals_module

        patch = mock.patch.object(goals_module, "judge_goal", side_effect=reply)
        self.judge = patch.start()
        self.addCleanup(patch.stop)

    def _transform(self, sid, result=_DONE, args=None):
        return self.plugin._transform_delegation_result(
            tool_name="delegate_task", args={"goal": "do it"} if args is None else args,
            result=result, session_id=sid, duration_ms=10, status="ok")

    def test_pre_tool_call_embeds_contract_when_goal_active(self):
        import copy

        self._seed("pre-active")
        args = {"tasks": [{"goal": "task A", "context": "ctx A", "toolsets": ["web"]},
                          {"goal": "task B", "toolsets": ["file"]}]}
        before = copy.deepcopy(args)
        out = self.plugin._pre_tool_call_finish_line(tool_name="delegate_task", args=args, session_id="pre-active")
        self.assertEqual(args, before)
        self.assertEqual(out["action"], "modify")
        tasks = out["args"]["tasks"]
        self.assertEqual(len(tasks), 2)
        for task, original in zip(tasks, before["tasks"]):
            self.assertIn("the original goal", task["goal"])
            self.assertIn("1. one", task["goal"])
            self.assertIn("$ true", task["goal"])
            self.assertIn(self.plugin._FINISH_LINE_TAIL, task["goal"])
            self.assertTrue(task["goal"].endswith(original["goal"]))
            self.assertEqual(task["toolsets"], original["toolsets"])
        self.assertTrue(tasks[0]["context"].startswith("ctx A"))
        self.assertNotIn("context", tasks[1])
        single = self.plugin._pre_tool_call_finish_line(
            tool_name="delegate_task", args={"goal": "solo"}, session_id="pre-active")
        self.assertEqual(set(single["args"]), {"goal"})
        self.assertTrue(single["args"]["goal"].endswith("\n\nsolo"))
        self.assertIsNone(self.plugin._pre_tool_call_finish_line(
            tool_name="delegate_task", args={"action": "continue", "subagent_id": "x", "message": "go"},
            session_id="pre-active"))

    def test_pre_tool_call_passthrough_no_goal(self):
        args = {"tasks": [{"goal": "task A"}]}
        out = self.plugin._pre_tool_call_finish_line(tool_name="delegate_task", args=args, session_id="pre-none")
        self.assertIsNone(out)
        self.assertEqual(args, {"tasks": [{"goal": "task A"}]})

    def test_pre_tool_call_ignores_other_tools(self):
        self._seed("pre-other")
        self.assertIsNone(self.plugin._pre_tool_call_finish_line(
            tool_name="terminal", args={"goal": "x"}, session_id="pre-other"))

    def test_result_shape_probe(self):
        self.assertEqual(self.plugin._delegation_summaries(_DONE), "child did the thing")
        self.assertEqual(self.plugin._delegation_summaries(json.loads(_DONE)), "child did the thing")
        ack = json.dumps({"status": "dispatched", "mode": "background", "subagent_ids": ["a"]})
        running = json.dumps({"results": [{"task_index": 0, "status": "running", "summary": ""}]})
        failed = json.dumps({"results": [{"task_index": 0, "status": "error", "summary": "boom"}]})
        for shape in (ack, running, failed, "not json", None):
            self.assertIsNone(self.plugin._delegation_summaries(shape), shape)

    def test_transform_gate_rejects_and_rewrites(self):
        self._seed("tr-reject")
        self._judge(lambda *a, **k: ("continue", "missing evidence", False, None, False))
        out = self._transform("tr-reject")
        self.assertIn("GOAL JUDGE: delegation NOT accepted", out)
        self.assertIn("missing evidence", out)
        self.assertIn("action='continue'", out)
        _, kwargs = self.judge.call_args
        self.assertEqual(kwargs["subgoals"], ["one", "two", "three"])
        self.assertEqual(self._sidecar("tr-reject")["delegation_retries"], 1)

    def test_transform_gate_accepts_passthrough(self):
        self._seed("tr-accept")
        self._judge(lambda *a, **k: ("done", "all criteria met", False, None, False))
        out = self._transform("tr-accept")
        self.assertIn("accepted", out)
        self.assertIn("child did the thing", out)
        self.assertTrue(out.startswith(_DONE))

    def test_transform_gate_retry_cap(self):
        self._seed("tr-cap")
        self._judge(lambda *a, **k: ("continue", "still weak", False, None, False))
        cont = {"action": "continue", "subagent_id": "sa-1", "message": "again"}
        first = self._transform("tr-cap")
        second = self._transform("tr-cap", args=cont)
        third = self._transform("tr-cap", args=cont)
        self.assertIn("NOT accepted", first)
        self.assertIn("NOT accepted", second)
        self.assertNotIn("NOT accepted", third)
        self.assertIn("⚠", third)
        self.assertIn("child did the thing", third)
        self.assertEqual(self._sidecar("tr-cap")["delegation_retries"], 2)

    def test_transform_gate_skips_async_ack(self):
        self._seed("tr-ack")
        self._judge(lambda *a, **k: ("continue", "x", False, None, False))
        ack = json.dumps({"status": "dispatched", "mode": "background", "subagent_ids": ["a"]})
        self.assertIsNone(self._transform("tr-ack", result=ack))
        self.judge.assert_not_called()

    def test_transform_gate_skips_no_goal(self):
        self._judge(lambda *a, **k: ("continue", "x", False, None, False))
        self.assertIsNone(self._transform("tr-none"))
        self.judge.assert_not_called()

    def test_transform_gate_error_failopen(self):
        self._seed("tr-error")

        def boom(*_a, **_k):
            raise RuntimeError("aux down")

        self._judge(boom)
        self.assertIsNone(self._transform("tr-error"))
        self.judge.side_effect = lambda *a, **k: ("continue", "transport", False, None, True)
        self.assertIsNone(self._transform("tr-error"))
        self.assertEqual(self._sidecar("tr-error")["delegation_retries"], 0)


if __name__ == "__main__":
    unittest.main()
