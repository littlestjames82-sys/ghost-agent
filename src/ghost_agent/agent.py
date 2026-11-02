"""The agent loop.

create task -> plan -> per step: propose tool call -> governance check
-> approval if needed -> RECORD TRAIL -> execute -> verify -> next.

Record-before-execute is a hard rule: the trail record for a tool
execution is written (flush + fsync) BEFORE the tool runs. If that
write fails, the action does NOT execute and the run fails honestly.
"""
from __future__ import annotations

from pathlib import Path

from . import governance
from .models import Router
from .skills import Skills
from .store import Store, new_id
from .tools import Tools, ToolError


class Agent:
    def __init__(self, store: Store, router: Router | None = None,
                 workspace: str | Path | None = None):
        self.store = store
        self.router = router or Router()
        self.tools = Tools(workspace or store.workspace)
        self.skills = Skills(store)

    # ---- task creation ---------------------------------------------------
    def create_task(self, goal: str, title: str | None = None) -> dict:
        planned = self.router.plan(goal)
        steps = []
        for s in planned["steps"]:
            steps.append({
                "tool": s.get("tool"),
                "args": s.get("args", {}),
                "verify": s.get("verify", {"type": "ok"}),
                "status": "pending",  # pending|waiting_approval|done|failed
                "approved": False,
                "result": None,
                "error": None,
            })
        usage = planned.get("usage") or {
            "model": planned["model"], "source": "mock",
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "estimated_cost_usd": 0.0, "cost_status": "not_applicable",
        }
        usage_model = usage.get("model") or planned["model"]
        task = {
            "id": new_id("tsk"),
            "title": title or goal[:60],
            "goal": goal,
            "status": "pending",  # pending|running|waiting_approval|done|failed
            "steps": steps,
            "current_step": 0,
            "model": planned["model"],
            "fallback_reason": planned["fallback_reason"],
            "skill_id": None,
            "error": None,
            # v0.6.0 usage accounting: cumulative totals for the
            # (single, planning) model call behind this task, plus a
            # per-model breakdown. MockModel records honest zeros.
            "usage_totals": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "calls": 1,
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "cost_status": usage.get("cost_status"),
            },
            "usage_by_model": {usage_model: {
                "calls": 1,
                "source": usage.get("source"),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
                "cost_status": usage.get("cost_status"),
            }},
            "usage_source": usage.get("source"),
        }
        task = self.store.put("tasks", task)
        if planned["fallback_reason"]:
            self.store.try_trail("model_fallback",
                                 {"reason": planned["fallback_reason"],
                                  "model": planned["model"]},
                                 task_id=task["id"])
        # One trail event per model call, carrying the usage numbers.
        self.store.try_trail("model_usage", dict(usage), task_id=task["id"])
        self.store.try_trail("task_created", {"goal": goal}, task_id=task["id"])
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

    # ---- the loop ------------------------------------------------------------
    def _fail_step(self, task: dict, idx: int, reason: str) -> dict:
        steps = task["steps"]
        steps[idx]["status"] = "failed"
        steps[idx]["error"] = reason
        return self.store.update("tasks", task["id"], steps=steps,
                                 status="failed", error=reason)

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
        steps = task["steps"]
        idx = task.get("current_step", 0)
        while idx < len(steps):
            step = steps[idx]
            if step["status"] == "done":
                idx += 1
                continue
            tool = step.get("tool")
            decision = governance.classify(tool or "")
            if decision.action == "deny":
                self.store.try_trail("governance_deny", {"reason": decision.reason},
                                     task_id=task_id, step_index=idx, tool=tool)
                task = self.store.get("tasks", task_id)
                return self._fail_step(task, idx, decision.reason)
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
                                         status="waiting_approval", current_step=idx)
                self.store.try_trail("approval_requested",
                                     {"approval_id": approval["id"], "reason": decision.reason},
                                     task_id=task_id, step_index=idx, tool=tool)
                return task
            # RECORD BEFORE EXECUTE — mandatory. If this raises, no execution.
            try:
                self.store.append_trail(
                    "tool_execute",
                    {"args": step.get("args", {}), "kind": decision.kind},
                    task_id=task_id, step_index=idx, tool=tool)
            except OSError as exc:
                task = self.store.get("tasks", task_id)
                return self._fail_step(
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
                                    "output": (result.get("output") or "")[:2000],
                                    "exit_code": result.get("exit_code")}
            if not ok:
                steps[idx]["status"] = "failed"
                steps[idx]["error"] = why
                task = self.store.update("tasks", task_id, steps=steps,
                                         status="failed", current_step=idx,
                                         error=f"step {idx} ({tool}) failed verification: {why}")
                self.store.try_trail("step_failed", {"reason": why},
                                     task_id=task_id, step_index=idx, tool=tool)
                return task
            steps[idx]["status"] = "done"
            idx += 1
            task = self.store.update("tasks", task_id, steps=steps, current_step=idx)
            self.store.try_trail("step_done", {}, task_id=task_id,
                                 step_index=idx - 1, tool=tool)
        task = self.store.update("tasks", task_id, status="done", current_step=idx)
        # Fully successful run -> propose a candidate (untrusted) skill.
        skill = self.skills.propose_from_run(task)
        task = self.store.update("tasks", task_id, skill_id=skill["id"])
        self.store.try_trail("task_done", {"skill_id": skill["id"]}, task_id=task_id)
        return task
