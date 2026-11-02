"""The agent loop (v0.7.0: plan-act-observe).

create task -> ask the model for the NEXT step -> governance check
-> approval if needed -> RECORD TRAIL -> execute -> observe -> ask
again with the history (including step outputs) in context -> ...
until the model answers "done", a guard stops the run, or a step
fails.

Planning one step at a time is the point: a step is planned with
the ACTUAL outputs of the steps before it in view, so a write can
use what a read found (v0.6.0's single-shot plan composed writes
before their reads ran — the summaries came out ungrounded).

Guards: MAX_STEPS executed steps per task; identical-step loop
detection; a per-task cap on argument-validation retries — a step
whose args fail validation is rejected, the error is fed back in
the history, and the model may correct it (the live gauntlet on
v0.6.0 lost 3/10 goals to one wrong argument name). Fallback
honesty: only a task's FIRST model call may fall back to
MockModel; a later provider failure fails the task with the
reason recorded — the brain never changes mid-task.

Token budget (v0.9.0): a task's total recorded provider tokens
are capped by GHOST_AGENT_TOKEN_BUDGET (default 100000; 0 or
negative disables). The gauntlet showed why: the loop re-sends
its history on every call, so document-heavy goals cost
35k-45k tokens each and nothing stopped a run from spending on.
When a call's usage brings the total to the budget and the task
still has work, the task fails BEFORE any further model call or
tool execution, with the reason in the trail. MockModel records
zero usage and never trips it — the budget guards provider
spend, and only provider spend.

Record-before-execute is a hard rule: the trail record for a tool
execution is written (flush + fsync) BEFORE the tool runs. If that
write fails, the action does NOT execute and the run fails honestly.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import governance
from .ecosystem import get_ecosystem
from .models import ModelError, Router
from .skills import Skills
from .store import Store, new_id
from .tools import Tools, ToolError, validate_args

MAX_STEPS = 12            # executed steps per task, hard cap
VALIDATION_RETRY_CAP = 2  # corrected-step requests per task
HISTORY_OUTPUT_CHARS = 4000   # per-step output kept in history
HISTORY_TOTAL_CHARS = 40000   # total history budget, outputs elided first
STEP_OUTPUT_CHARS = 8000      # per-step output stored on the task record
TOKEN_BUDGET_ENV = "GHOST_AGENT_TOKEN_BUDGET"
DEFAULT_TOKEN_BUDGET = 100_000  # per-task provider tokens, when unset


def configured_token_budget() -> int | None:
    """The per-task provider-token budget from
    GHOST_AGENT_TOKEN_BUDGET: an integer cap, defaulting to
    DEFAULT_TOKEN_BUDGET when unset. 0 or negative disables the
    guardrail (returns None); an unparseable value falls back to
    the default rather than crashing startup."""
    raw = os.environ.get(TOKEN_BUDGET_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_TOKEN_BUDGET
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_TOKEN_BUDGET
    return value if value > 0 else None


class Agent:
    def __init__(self, store: Store, router: Router | None = None,
                 workspace: str | Path | None = None):
        self.store = store
        self.router = router or Router()
        # The ecosystem registry (v0.8.0) backs the read-class
        # project tools and MockModel's 'status of <project>'
        # pattern. One instance per store, shared with the
        # dashboard via get_ecosystem.
        self.ecosystem = get_ecosystem(store)
        self.tools = Tools(workspace or store.workspace,
                           ecosystem=self.ecosystem)
        # The per-task token budget (v0.9.0), resolved from the
        # environment at construction; None when disabled. It is
        # stamped onto every task this agent creates.
        self.token_budget = configured_token_budget()
        mock = getattr(self.router, "mock", None)
        if mock is not None:
            mock.ecosystem = self.ecosystem
        self.skills = Skills(store)

    # ---- steps + usage bookkeeping --------------------------------------
    @staticmethod
    def _step_record(step: dict) -> dict:
        return {
            "tool": step.get("tool"),
            "args": step.get("args", {}),
            "verify": step.get("verify", {"type": "ok"}),
            "status": "pending",  # pending|waiting_approval|done|failed|rejected
            "approved": False,
            "result": None,
            "error": None,
        }

    @staticmethod
    def _merge_cost(entry: dict, usage: dict) -> None:
        cost = usage.get("estimated_cost_usd")
        if usage.get("source") == "provider" and cost is None:
            entry["estimated_cost_usd"] = None
            entry["cost_status"] = "unknown"
        elif entry.get("cost_status") == "unknown":
            pass  # once unpriced, a priced total can no longer be claimed
        elif cost is not None:
            entry["estimated_cost_usd"] = round(
                (entry.get("estimated_cost_usd") or 0.0) + cost, 6)
            entry["cost_status"] = usage.get("cost_status") \
                or entry.get("cost_status")

    def _apply_usage(self, task: dict, usage: dict) -> None:
        """Accumulate one model call's usage record into the
        task's cumulative totals + per-model breakdown (in place;
        the caller persists). Mirrors server._usage_summary's
        honesty rules: any unpriced provider call makes the total
        unknown, never a partial sum presented as the total."""
        totals = task["usage_totals"]
        totals["calls"] += 1
        totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += usage.get("completion_tokens", 0)
        totals["total_tokens"] += usage.get("total_tokens", 0)
        name = usage.get("model") or task.get("model") or "unknown"
        entry = task["usage_by_model"].setdefault(name, {
            "calls": 0, "source": usage.get("source"),
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "estimated_cost_usd": 0.0, "cost_status": "not_applicable"})
        entry["calls"] += 1
        entry["source"] = usage.get("source")
        entry["prompt_tokens"] += usage.get("prompt_tokens", 0)
        entry["completion_tokens"] += usage.get("completion_tokens", 0)
        entry["total_tokens"] += usage.get("total_tokens", 0)
        self._merge_cost(entry, usage)
        task["usage_source"] = usage.get("source")
        by_model = task["usage_by_model"]
        if any(e["cost_status"] == "unknown" for e in by_model.values()):
            totals["estimated_cost_usd"] = None
            totals["cost_status"] = "unknown"
        elif any(e["cost_status"] == "estimated" for e in by_model.values()):
            totals["estimated_cost_usd"] = round(
                sum(e["estimated_cost_usd"] or 0.0
                    for e in by_model.values()), 6)
            totals["cost_status"] = "estimated"
        else:
            totals["estimated_cost_usd"] = 0.0
            totals["cost_status"] = "not_applicable"

    @staticmethod
    def _history(task: dict) -> list[dict]:
        """Completed steps as the model sees them: tool, args,
        status, and observed output (rejected steps carry their
        validation error as the output, so the model can correct
        itself). Each output is truncated; when the total exceeds
        the budget, the oldest outputs are elided (never the steps
        themselves) before any entry is dropped."""
        out = []
        for s in task.get("steps", []):
            status = s.get("status")
            if status not in ("done", "failed", "rejected"):
                continue
            if status == "rejected":
                output = f"validation error: {s.get('error') or 'invalid step'}"
            else:
                output = ((s.get("result") or {}).get("output")
                          or "")[:HISTORY_OUTPUT_CHARS]
            out.append({"tool": s.get("tool"), "args": s.get("args", {}),
                        "status": status, "output": output})
        # Over budget: elide the OUTPUT of the oldest entries first.
        # The step itself (tool + args + status) always stays
        # visible, so the model knows it already ran and does not
        # repeat it — the v0.7.0 gauntlet's step-cap failures all
        # showed the model re-listing and re-reading files whose
        # history entries had been dropped whole. Whole entries are
        # dropped only if the skeleton alone still exceeds budget.
        def _size(entries):
            return sum(len(str(e)) for e in entries)

        idx = 0
        while _size(out) > HISTORY_TOTAL_CHARS and idx < len(out) - 1:
            if out[idx]["output"]:
                out[idx] = {**out[idx], "output":
                            "[elided to save space - you already "
                            "completed this step; re-run it only if "
                            "you need a specific part of its output]"}
            idx += 1
        while len(out) > 1 and _size(out) > HISTORY_TOTAL_CHARS:
            out.pop(0)
        return out

    # ---- task creation ---------------------------------------------------
    def create_task(self, goal: str, title: str | None = None) -> dict:
        # The task's first next-step call. This is the ONLY call
        # that may fall back to MockModel (Router policy); its
        # outcome fixes the task's brain for its whole life.
        first = self.router.next_step(goal, [], brain=None)
        steps = []
        if first["step"] is not None:
            steps.append(self._step_record(first["step"]))
        task = {
            "id": new_id("tsk"),
            "title": title or goal[:60],
            "goal": goal,
            "status": "pending",  # pending|running|waiting_approval|done|failed
            "steps": steps,
            "current_step": 0,
            "model": first["model"],
            "fallback_reason": first["fallback_reason"],
            "skill_id": None,
            "error": None,
            # v0.7.0 loop state.
            "planning_done": bool(first["done"]),
            "plan_calls": 1,
            "loop_repeats": 0,
            "validation_retries": 0,
            # The token budget this task runs under (v0.9.0):
            # the configured cap, or None when disabled.
            "token_budget": self.token_budget,
            # Usage accounting (v0.6.0 shape): cumulative across
            # ALL of the task's model calls, plus per-model detail.
            "usage_totals": {
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "calls": 0,
                "estimated_cost_usd": 0.0,
                "cost_status": "not_applicable",
            },
            "usage_by_model": {},
            "usage_source": None,
        }
        if first["usage"] is not None:
            self._apply_usage(task, first["usage"])
        # A first call can already exhaust the budget (one huge
        # provider answer). If the task still has work, it fails
        # here — before any tool executes.
        budget_failed = bool(not first["done"]
                             and self._budget_tripped(task))
        if budget_failed:
            task["status"] = "failed"
            task["error"] = self._budget_reason(task)
        task = self.store.put("tasks", task)
        self.store.try_trail(
            "replan",
            {"iteration": 0,
             "tool": (first["step"] or {}).get("tool") or "done"},
            task_id=task["id"])
        if first["fallback_reason"]:
            self.store.try_trail("model_fallback",
                                 {"reason": first["fallback_reason"],
                                  "model": first["model"]},
                                 task_id=task["id"])
        if first["usage"] is not None:
            self.store.try_trail("model_usage", dict(first["usage"]),
                                 task_id=task["id"])
        self.store.try_trail("task_created", {"goal": goal},
                             task_id=task["id"])
        if budget_failed:
            self.store.try_trail(
                "budget_exceeded",
                {"used": task["usage_totals"]["total_tokens"],
                 "budget": task["token_budget"]},
                task_id=task["id"])
        return task

    # ---- approvals ---------------------------------------------------------
    def _pending_approval(self, task_id: str, step_index: int) -> dict | None:
        for a in self.store.list("approvals"):
            if (a.get("task_id") == task_id and a.get("step_index") == step_index
                    and a.get("status") == "pending"):
                return a
        return None

    def pending_approvals(self) -> list[dict]:
        return [a for a in self.store.list("approvals") if a.get("status") == "pending"]

    def approve_task(self, task_id: str) -> dict | None:
        task = self.store.get("tasks", task_id)
        if task is None:
            return None
        approval = self._pending_approval(task_id, task.get("current_step", 0))
        if approval is None:
            return task
        self.store.update("approvals", approval["id"], status="approved")
        steps = task["steps"]
        steps[task["current_step"]]["approved"] = True
        steps[task["current_step"]]["status"] = "pending"
        self.store.update("tasks", task_id, steps=steps, status="running")
        self.store.try_trail("approval_granted", {"approval_id": approval["id"]},
                             task_id=task_id, step_index=task["current_step"],
                             tool=approval.get("tool"))
        return self.run_task(task_id)

    def deny_task(self, task_id: str) -> dict | None:
        task = self.store.get("tasks", task_id)
        if task is None:
            return None
        approval = self._pending_approval(task_id, task.get("current_step", 0))
        if approval is not None:
            self.store.update("approvals", approval["id"], status="denied")
        steps = task["steps"]
        idx = task.get("current_step", 0)
        if idx < len(steps):
            steps[idx]["status"] = "failed"
            steps[idx]["error"] = "denied by human"
        task = self.store.update("tasks", task_id, steps=steps, status="failed",
                                 error="step denied by human; run stopped")
        self.store.try_trail("approval_denied",
                             {"approval_id": approval["id"] if approval else None},
                             task_id=task_id, step_index=idx)
        return task

    # ---- failure helpers ---------------------------------------------------
    def _fail_step(self, task: dict, idx: int, reason: str) -> dict:
        steps = task["steps"]
        steps[idx]["status"] = "failed"
        steps[idx]["error"] = reason
        return self.store.update("tasks", task["id"], steps=steps,
                                 status="failed", error=reason)

    def _fail_loop(self, task: dict, reason: str, event_type: str) -> dict:
        """Fail a task for a loop-level reason (guard, validation
        cap, model failure), with the reason in the trail."""
        task = self.store.update("tasks", task["id"], status="failed",
                                 error=reason)
        self.store.try_trail(event_type, {"reason": reason},
                             task_id=task["id"])
        return task

    # ---- token budget (v0.9.0) -------------------------------------------
    @staticmethod
    def _budget_tripped(task: dict) -> bool:
        """True when the task carries an enabled budget and its
        recorded provider usage has reached it. MockModel usage is
        zero, so mock tasks never trip — by design."""
        budget = task.get("token_budget")
        if not budget:
            return False
        used = (task.get("usage_totals") or {}).get("total_tokens", 0)
        return used >= budget

    @staticmethod
    def _budget_reason(task: dict) -> str:
        used = (task.get("usage_totals") or {}).get("total_tokens", 0)
        return f"token budget reached ({used} of {task.get('token_budget')} tokens)"

    def _fail_budget(self, task: dict) -> dict:
        """Fail a task that reached its token budget, with a
        dedicated budget_exceeded trail event carrying the
        used/budget pair."""
        used = (task.get("usage_totals") or {}).get("total_tokens", 0)
        budget = task.get("token_budget")
        task = self.store.update("tasks", task["id"], status="failed",
                                 error=self._budget_reason(task))
        self.store.try_trail("budget_exceeded",
                             {"used": used, "budget": budget},
                             task_id=task["id"])
        return task

    def _reject_step(self, task: dict, idx: int, problem: str):
        """Argument-validation feedback: park the bad proposal as
        a rejected step (its error becomes history), and let the
        loop ask for a corrected step — up to the per-task cap,
        after which the task fails honestly with the same error."""
        task_id = task["id"]
        steps = task["steps"]
        steps[idx]["status"] = "rejected"
        steps[idx]["error"] = problem
        steps[idx]["result"] = {"ok": False, "output": "", "exit_code": None}
        retries = task.get("validation_retries", 0)
        if retries >= VALIDATION_RETRY_CAP:
            self.store.update("tasks", task_id, steps=steps,
                              current_step=idx + 1)
            task = self.store.get("tasks", task_id)
            return "failed", self._fail_loop(task, problem,
                                             "validation_failed")
        self.store.update("tasks", task_id, steps=steps,
                          current_step=idx + 1,
                          validation_retries=retries + 1)
        self.store.try_trail("step_rejected", {"reason": problem},
                             task_id=task_id, step_index=idx,
                             tool=steps[idx].get("tool"))
        return "rejected", self.store.get("tasks", task_id)

    # ---- verification --------------------------------------------------------
    def _verify(self, step: dict, result: dict) -> tuple[bool, str]:
        if not result.get("ok"):
            return False, result.get("error") or f"tool exited {result.get('exit_code')}"
        verify = step.get("verify") or {"type": "ok"}
        vtype = verify.get("type", "ok")
        if vtype == "ok":
            return True, ""
        if vtype == "exit_zero":
            return (result.get("exit_code") == 0), "exit code was not 0"
        if vtype == "file_content":
            p = self.tools._resolve(verify["path"])
            if not p.is_file():
                return False, f"expected file missing after run: {verify['path']}"
            actual = p.read_text(encoding="utf-8", errors="replace")
            return (actual == verify.get("content", "")), "file content did not match expected"
        if vtype == "output_contains":
            return (verify.get("text", "") in result.get("output", "")), "output missing expected text"
        return True, ""

    # ---- the loop ------------------------------------------------------------
    def _execute_pending(self, task: dict):
        """Execute every currently-planned step from current_step.
        Returns (signal, task): "exhausted" (all planned steps are
        done), "rejected" (a step was rejected for bad args — the
        plan may grow a corrected step), "paused" (waiting on a
        human), or "failed" (the task is finished, badly)."""
        task_id = task["id"]
        steps = task["steps"]
        idx = task.get("current_step", 0)
        while idx < len(steps):
            step = steps[idx]
            if step["status"] in ("done", "rejected"):
                idx += 1
                continue
            tool = step.get("tool")
            decision = governance.classify(tool or "")
            if decision.action == "deny":
                self.store.try_trail("governance_deny", {"reason": decision.reason},
                                     task_id=task_id, step_index=idx, tool=tool)
                task = self.store.get("tasks", task_id)
                return "failed", self._fail_step(task, idx, decision.reason)
            # Argument validation BEFORE any approval prompt: a
            # step that cannot run must not consume a human
            # decision. Only loop-created tasks (plan_calls set)
            # get the feedback path; pre-seeded tasks keep the
            # v0.6.0 execute-and-fail semantics.
            problem = validate_args(tool, step.get("args"))
            if problem is not None and task.get("plan_calls") is not None:
                return self._reject_step(task, idx, problem)
            if decision.action == "approval" and not step.get("approved"):
                approval = self._pending_approval(task_id, idx)
                if approval is None:
                    approval = self.store.put("approvals", {
                        "id": new_id("apr"), "task_id": task_id, "step_index": idx,
                        "tool": tool, "args": step.get("args", {}),
                        "status": "pending", "reason": decision.reason,
                    })
                steps[idx]["status"] = "waiting_approval"
                task = self.store.update("tasks", task_id, steps=steps,
                                         status="waiting_approval",
                                         current_step=idx)
                self.store.try_trail("approval_requested",
                                     {"approval_id": approval["id"], "reason": decision.reason},
                                     task_id=task_id, step_index=idx, tool=tool)
                return "paused", task
            # RECORD BEFORE EXECUTE — mandatory. If this raises, no execution.
            try:
                self.store.append_trail(
                    "tool_execute",
                    {"args": step.get("args", {}), "kind": decision.kind},
                    task_id=task_id, step_index=idx, tool=tool)
            except OSError as exc:
                task = self.store.get("tasks", task_id)
                return "failed", self._fail_step(
                    task, idx,
                    f"trail write failed ({type(exc).__name__}); action NOT executed")
            try:
                result = self.tools.execute(tool, step.get("args", {}))
            except ToolError as exc:
                result = {"ok": False, "error": str(exc), "output": ""}
            ok, why = self._verify(step, result)
            task = self.store.get("tasks", task_id)
            steps = task["steps"]
            steps[idx]["result"] = {"ok": result.get("ok"),
                                    "output": (result.get("output") or "")[:STEP_OUTPUT_CHARS],
                                    "exit_code": result.get("exit_code")}
            if not ok:
                steps[idx]["status"] = "failed"
                steps[idx]["error"] = why
                task = self.store.update("tasks", task_id, steps=steps,
                                         status="failed", current_step=idx,
                                         error=f"step {idx} ({tool}) failed verification: {why}")
                self.store.try_trail("step_failed", {"reason": why},
                                     task_id=task_id, step_index=idx, tool=tool)
                return "failed", task
            steps[idx]["status"] = "done"
            idx += 1
            task = self.store.update("tasks", task_id, steps=steps,
                                     current_step=idx)
            self.store.try_trail("step_done", {}, task_id=task_id,
                                 step_index=idx - 1, tool=tool)
        return "exhausted", self.store.get("tasks", task_id) or task

    def run_task(self, task_id: str) -> dict | None:
        task = self.store.get("tasks", task_id)
        if task is None:
            return None
        if task.get("imported") or task.get("archived"):
            # Imported history is never re-run (v0.6.0 import safety).
            return task
        if task["status"] in ("done", "failed"):
            return task
        task = self.store.update("tasks", task_id, status="running")
        while True:
            signal, task = self._execute_pending(task)
            if signal in ("paused", "failed"):
                return task
            # "exhausted" / "rejected": everything planned has run
            # (or been rejected). Finish, or grow the plan by one
            # step planned with the full history in view.
            task = self.store.get("tasks", task_id)
            if task.get("planning_done"):
                break
            executed = [s for s in task["steps"] if s["status"] == "done"]
            if len(executed) >= MAX_STEPS:
                return self._fail_loop(
                    task, f"step cap reached ({MAX_STEPS} executed steps)",
                    "loop_guard")
            iteration = task.get("plan_calls", 0)
            self.store.try_trail("replan", {"iteration": iteration},
                                 task_id=task_id)
            try:
                nxt = self.router.next_step(task["goal"],
                                            self._history(task),
                                            brain=task.get("model"))
            except ModelError as exc:
                # Mid-task provider failure: fail honestly. The
                # brain never changes mid-task.
                return self._fail_loop(task, f"model call failed: {exc}",
                                       "model_call_failed")
            updates: dict = {"plan_calls": iteration + 1}
            if nxt["usage"] is not None:
                self._apply_usage(task, nxt["usage"])
                updates["usage_totals"] = task["usage_totals"]
                updates["usage_by_model"] = task["usage_by_model"]
                updates["usage_source"] = task["usage_source"]
            # Token budget (v0.9.0): this call's usage is recorded;
            # if it reached the budget and the model still has a
            # step to run, stop NOW — before that step executes and
            # before any further model call. A "done" answer is
            # not tripped: the work is finished, not cut off.
            if nxt["step"] is not None and self._budget_tripped(task):
                task = self.store.update("tasks", task_id, **updates)
                if nxt["usage"] is not None:
                    self.store.try_trail("model_usage",
                                         dict(nxt["usage"]),
                                         task_id=task_id)
                return self._fail_budget(task)
            if nxt["done"] or nxt["step"] is None:
                updates["planning_done"] = True
                task = self.store.update("tasks", task_id, **updates)
                if nxt["usage"] is not None:
                    self.store.try_trail("model_usage",
                                         dict(nxt["usage"]),
                                         task_id=task_id)
                break
            step = self._step_record(nxt["step"])
            # Loop detection: the same (tool, args) as the last
            # executed step, proposed twice in a row, is a spin.
            prev = executed[-1] if executed else None
            repeats = task.get("loop_repeats", 0)
            if (prev is not None and prev.get("tool") == step["tool"]
                    and (prev.get("args") or {}) == (step["args"] or {})):
                repeats += 1
                if repeats >= 2:
                    self.store.update("tasks", task_id,
                                      loop_repeats=repeats, **updates)
                    if nxt["usage"] is not None:
                        self.store.try_trail("model_usage",
                                             dict(nxt["usage"]),
                                             task_id=task_id)
                    task = self.store.get("tasks", task_id)
                    return self._fail_loop(
                        task,
                        f"loop detected: model proposed the same step "
                        f"({step['tool']}) repeatedly",
                        "loop_guard")
            else:
                repeats = 0
            updates["loop_repeats"] = repeats
            steps = task["steps"]
            steps.append(step)
            updates["steps"] = steps
            task = self.store.update("tasks", task_id, **updates)
            if nxt["usage"] is not None:
                self.store.try_trail("model_usage", dict(nxt["usage"]),
                                     task_id=task_id)
        task = self.store.update("tasks", task_id, status="done",
                                 current_step=len(task["steps"]))
        # Fully successful run -> propose a candidate (untrusted) skill.
        skill = self.skills.propose_from_run(task)
        task = self.store.update("tasks", task_id, skill_id=skill["id"])
        self.store.try_trail("task_done", {"skill_id": skill["id"]},
                             task_id=task_id)
        return task
