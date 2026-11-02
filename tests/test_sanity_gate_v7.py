"""Ghost Agent v7 SANITY GATE — known answers, run FIRST.

K1 Back-compat: v1..v6 gates (30 tests) pass in the same run
   (subprocess).
K2 Grounding (plan-act-observe): a scripted stub provider answers
   read_file first; its second call MUST receive the read step's
   actual output in the history (asserted inside the stub), answers
   write_file quoting that output, then done. The written file
   contains the seeded text, usage totals are the exact sum of the
   stub's per-call usage, and a replan event exists per iteration.
K3 Approval inside the loop: read -> write -> done. The write
   pauses the task (file absent) and NO provider call happens
   while paused (stub call count frozen); approve -> loop resumes
   -> done.
K4 Loop safety: a stub that always proposes the identical step
   fails with loop detection (few executed steps); a stub that
   always proposes fresh steps fails at the 12-step cap with the
   cap reason in the trail.
K5 Mid-loop provider failure: first call OK (a read), second call
   raises -> task failed with the provider reason in the trail,
   the task's model stays the provider's (no mid-task MockModel
   substitution), no skill proposed.
K6 MockModel regression + muse-spark pricing: demo, write, and
   find tasks via MockModel behave exactly as v0.6.0 (files,
   statuses, skill proposals, zero usage); the muse-spark family
   is priced at $1.25/$4.25 per million (1M + 1M -> $5.50) and a
   muse-spark usage record reports cost_status "estimated".
K7 Validation feedback (from the live 10-goal gauntlet on v0.6.0,
   where 3/10 goals died on "missing args for search_files:
   ['query']"): a proposed step with wrong argument names must
   NOT fail the task — the validation error goes into the history
   and the model gets to correct it (the stub asserts the error
   text is in the next call's prompt, then answers with the
   corrected step, then done -> task completes). A stub that
   never corrects fails the task after the 2-retry cap with the
   validation reason in the trail. The next-step prompt must also
   carry the tools' exact argument signatures.
"""
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from ghost_agent.agent import Agent
from ghost_agent.models import (OpenAICompatibleModel, Router,
                                estimate_cost_usd, usage_record)
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent

SEEDED = "QUOKKA-7391 distinctive seeded line"


# ---- K1 -----------------------------------------------------------------
def test_k1_prior_gates_still_pass():
    import os
    full_env = dict(os.environ, PYTHONPATH="src",
                    PATH="/usr/bin:/bin:/usr/local/bin")
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py",
         "tests/test_sanity_gate_v5.py", "tests/test_sanity_gate_v6.py",
         "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=900)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "30 passed" in out.stdout


# ---- stub provider plumbing ----------------------------------------------
class _StubResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _step_payload(step, usage, model="stub-loop-model"):
    return {"model": model,
            "choices": [{"message": {"content": json.dumps(step)}}],
            "usage": usage}


class _ScriptedStub:
    """urlopen replacement: hands out one scripted answer per call
    and records every request prompt it receives."""

    def __init__(self, script):
        self.script = script          # fn(call_no, prompt) -> payload | raise
        self.prompts = []

    @property
    def calls(self):
        return len(self.prompts)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        prompt = body["messages"][0]["content"]
        self.prompts.append(prompt)
        return _StubResponse(self.script(len(self.prompts), prompt))


def _stub_agent(tmp_path, stub, monkeypatch, name="home"):
    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen", stub)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", "gate-v7-dummy-key")
    store = Store(tmp_path / name)
    router = Router(prefer_real=True)
    router.real = OpenAICompatibleModel(base_url="http://stub.invalid/v1",
                                        model="stub-loop-model")
    return store, Agent(store, router=router)


# ---- K2 -----------------------------------------------------------------
def test_k2_grounding_loop(tmp_path, monkeypatch):
    def script(n, prompt):
        if n == 1:
            return _step_payload(
                {"tool": "read_file", "args": {"path": "source.txt"},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 10, "completion_tokens": 5,
                 "total_tokens": 15})
        if n == 2:
            # The whole point of the loop: this call's prompt must
            # carry the read step's actual output.
            assert SEEDED in prompt, "second call did not observe the read"
            return _step_payload(
                {"tool": "write_file",
                 "args": {"path": "grounded.txt",
                          "content": f"copied: {SEEDED}"},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 20, "completion_tokens": 7,
                 "total_tokens": 27})
        return _step_payload({"tool": "done"},
                             {"prompt_tokens": 3, "completion_tokens": 2,
                              "total_tokens": 5})

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "source.txt").write_text(SEEDED + "\n",
                                                encoding="utf-8")
    task = agent.create_task("read source.txt and write a grounded copy")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"       # the write pauses
    task = agent.approve_task(task["id"])
    assert task["status"] == "done"
    out = (store.workspace / "grounded.txt").read_text(encoding="utf-8")
    assert SEEDED in out
    totals = task["usage_totals"]
    assert totals["prompt_tokens"] == 33              # 10 + 20 + 3
    assert totals["completion_tokens"] == 14          # 5 + 7 + 2
    assert totals["total_tokens"] == 47
    assert totals["calls"] == 3
    assert task["usage_by_model"]["stub-loop-model"]["total_tokens"] == 47
    usage_events = [e for e in store.read_trail(task_id=task["id"])
                    if e["type"] == "model_usage"]
    assert len(usage_events) == 3
    replans = [e for e in store.read_trail(task_id=task["id"])
               if e["type"] == "replan"]
    assert [e["detail"]["iteration"] for e in replans] == [0, 1, 2]
    assert task["fallback_reason"] is None


# ---- K3 -----------------------------------------------------------------
def test_k3_approval_inside_loop(tmp_path, monkeypatch):
    def script(n, prompt):
        if n == 1:
            return _step_payload(
                {"tool": "read_file", "args": {"path": "in.txt"},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10})
        if n == 2:
            return _step_payload(
                {"tool": "write_file",
                 "args": {"path": "out.txt", "content": "looped"},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10})
        return _step_payload({"tool": "done"},
                             {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2})

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "in.txt").write_text("seed\n", encoding="utf-8")
    task = agent.create_task("read in.txt then write out.txt")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    assert not (store.workspace / "out.txt").exists()
    frozen = stub.calls
    assert frozen == 2
    # Re-entering the run while paused must not consult the model.
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    assert stub.calls == frozen
    task = agent.approve_task(task["id"])
    assert task["status"] == "done"
    assert (store.workspace / "out.txt").read_text(encoding="utf-8") == "looped"
    assert stub.calls == 3


# ---- K4 -----------------------------------------------------------------
def test_k4_loop_detection(tmp_path, monkeypatch):
    def script(n, prompt):
        return _step_payload(
            {"tool": "echo_note", "args": {"text": "same"},
             "verify": {"type": "ok"}},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    task = agent.create_task("keep going")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert "loop detected" in (task["error"] or "")
    done_steps = [s for s in task["steps"] if s["status"] == "done"]
    assert len(done_steps) == 2                       # stopped early
    guards = [e for e in store.read_trail(task_id=task["id"])
              if e["type"] == "loop_guard"]
    assert guards and "loop detected" in guards[0]["detail"]["reason"]


def test_k4_step_cap(tmp_path, monkeypatch):
    def script(n, prompt):
        return _step_payload(
            {"tool": "echo_note", "args": {"text": f"fresh {n}"},
             "verify": {"type": "ok"}},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    task = agent.create_task("never finish")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert "step cap reached" in (task["error"] or "")
    done_steps = [s for s in task["steps"] if s["status"] == "done"]
    assert len(done_steps) == 12
    assert len(task["steps"]) == 12
    assert task["skill_id"] is None
    guards = [e for e in store.read_trail(task_id=task["id"])
              if e["type"] == "loop_guard"]
    assert guards and "step cap reached" in guards[0]["detail"]["reason"]


# ---- K5 -----------------------------------------------------------------
def test_k5_midloop_provider_failure(tmp_path, monkeypatch):
    def script(n, prompt):
        if n == 1:
            return _step_payload(
                {"tool": "read_file", "args": {"path": "k5.txt"},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10})
        raise urllib.error.URLError("provider exploded mid-loop")

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "k5.txt").write_text("x", encoding="utf-8")
    task = agent.create_task("read k5.txt then keep working")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert task["steps"][0]["status"] == "done"       # the read happened
    assert "provider request failed" in (task["error"] or "")
    # The brain never changed mid-task: no mock substitution.
    assert task["model"] == "openai-compatible"
    assert task["fallback_reason"] is None
    assert task["skill_id"] is None
    fails = [e for e in store.read_trail(task_id=task["id"])
             if e["type"] == "model_call_failed"]
    assert fails and "provider request failed" in fails[0]["detail"]["reason"]
    assert stub.calls == 2                            # no silent retry


# ---- K6 -----------------------------------------------------------------
def _mock_agent(tmp_path, name="home"):
    store = Store(tmp_path / name)
    return store, Agent(store, router=Router(prefer_real=False))


def test_k6_mock_regression_and_spark_pricing(tmp_path):
    store, agent = _mock_agent(tmp_path)
    # Demo: identical outcome to v0.6.0.
    task = agent.create_task("demo")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    task = agent.approve_task(task["id"])
    assert task["status"] == "done"
    assert [s["tool"] for s in task["steps"]] == \
        ["echo_note", "write_file", "read_file"]
    assert all(s["status"] == "done" for s in task["steps"])
    assert (store.workspace / "demo_output.txt").read_text(
        encoding="utf-8") == "Ghost Agent demo run\n"
    assert task["skill_id"]
    skill = store.get("skills", task["skill_id"])
    assert skill["status"] == "candidate" and skill["trusted"] is False
    assert task["usage_totals"]["total_tokens"] == 0
    assert task["fallback_reason"] is None

    # A plain write task: identical outcome to v0.6.0.
    t2 = agent.create_task("write k6.txt with: loop-proof")
    t2 = agent.run_task(t2["id"])
    assert t2["status"] == "waiting_approval"
    t2 = agent.approve_task(t2["id"])
    assert t2["status"] == "done"
    assert (store.workspace / "k6.txt").read_text(
        encoding="utf-8") == "loop-proof"

    # A find task: identical outcome to v0.6.0.
    (store.workspace / "hay.txt").write_text("a needle in a file\n",
                                             encoding="utf-8")
    t3 = agent.create_task("find needle in .")
    t3 = agent.run_task(t3["id"])
    assert t3["status"] == "done"
    assert t3["steps"][0]["tool"] == "search_files"
    assert "hay.txt:1" in t3["steps"][0]["result"]["output"]

    # muse-spark pricing (Meta's published rates, Oct 26, 2026).
    assert estimate_cost_usd("muse-spark-1.3", 1_000_000, 1_000_000) == \
        pytest.approx(5.50)
    assert estimate_cost_usd("muse-spark", 1_000_000, 0) == \
        pytest.approx(1.25)
    rec = usage_record("muse-spark-1.3", "provider",
                       {"prompt_tokens": 100, "completion_tokens": 100,
                        "total_tokens": 200})
    assert rec["cost_status"] == "estimated"
    assert rec["estimated_cost_usd"] == pytest.approx(
        (100 * 1.25 + 100 * 4.25) / 1_000_000)


# ---- K7 -----------------------------------------------------------------
def test_k7_validation_feedback_corrects(tmp_path, monkeypatch):
    seen = {"error_in_prompt": False}

    def script(n, prompt):
        if n == 1:
            # The gauntlet failure: right tool, wrong argument name.
            return _step_payload(
                {"tool": "search_files", "args": {"text": "needle"},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10})
        if n == 2:
            seen["error_in_prompt"] = \
                "missing args for search_files: ['query']" in prompt
            return _step_payload(
                {"tool": "search_files",
                 "args": {"query": "needle", "path": "."},
                 "verify": {"type": "ok"}},
                {"prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10})
        return _step_payload({"tool": "done"},
                             {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2})

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "hay.txt").write_text("a needle in a file\n",
                                             encoding="utf-8")
    # The prompt itself must teach the exact argument signatures.
    task = agent.create_task("find needle in the workspace")
    assert "search_files{query, path?}" in stub.prompts[0]
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert seen["error_in_prompt"], \
        "validation error was not fed back into the next call's history"
    rejected = [s for s in task["steps"] if s["status"] == "rejected"]
    assert len(rejected) == 1
    assert "missing args for search_files: ['query']" in rejected[0]["error"]
    executed = [s for s in task["steps"] if s["status"] == "done"]
    assert len(executed) == 1 and executed[0]["tool"] == "search_files"
    assert "hay.txt:1" in executed[0]["result"]["output"]
    assert task["validation_retries"] == 1
    rejections = [e for e in store.read_trail(task_id=task["id"])
                  if e["type"] == "step_rejected"]
    assert rejections and "missing args for search_files" in \
        rejections[0]["detail"]["reason"]


def test_k7_validation_feedback_cap(tmp_path, monkeypatch):
    def script(n, prompt):
        return _step_payload(
            {"tool": "search_files", "args": {"text": "needle"},
             "verify": {"type": "ok"}},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    task = agent.create_task("find needle in the workspace")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert "missing args for search_files: ['query']" in (task["error"] or "")
    # 1 initial proposal + 2 correction retries, then the cap bites.
    assert stub.calls == 3
    assert task["validation_retries"] == 2
    assert not [s for s in task["steps"] if s["status"] == "done"]
    reasons = [e["detail"].get("reason", "")
               for e in store.read_trail(task_id=task["id"])]
    assert any("missing args for search_files" in r for r in reasons)
