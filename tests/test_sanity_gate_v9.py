"""Ghost Agent v9 SANITY GATE — known answers, run FIRST.

v0.9.0 is three evidence-based fixes from the Nov 2 live gauntlet
and the ecosystem probes:

1. TOKEN BUDGET GUARDRAIL. The gauntlet's document-heavy goals
   cost 35k-45k tokens each because the loop re-sends its history
   on every call, and nothing stopped a run from spending on.
   GHOST_AGENT_TOKEN_BUDGET (integer, default 100000; 0 or
   negative disables) now caps a task's total provider tokens:
   once the recorded usage reaches the budget and the task still
   has work, it stops BEFORE any further model call or tool
   execution — failed with 'token budget reached (<used> of
   <budget> tokens)', a budget_exceeded trail event, no skill
   proposal — and the task record carries its 'token_budget'
   (null when disabled). MockModel records zero usage and never
   trips it: the budget guards provider spend, honestly.
2. repo_status FALLBACK FACTS. Most studio trees are API-synced
   copies with no .git, so v0.8.0's "not a git repository" was
   all repo_status could say about them. For a non-git tree it
   now also reports tree facts: recursive file count (bounded at
   10000, rendered '10000+'), the newest file modification time
   (ISO), and which of the usual root markers it carries
   (README*, BUILD_REPORT.md, pyproject.toml, package.json).
   The git-repo answer is unchanged.
3. FULLER STORED STEP OUTPUTS. A task record stored only the
   first 2000 chars of a step's output, so a 72-project
   list_projects result showed ~15 lines in the task detail.
   The stored cap is now 8000 chars. (The loop's history budget,
   4000/40000, is a different budget and is NOT changed.)

K1 Budget trips: stub provider, 60000 tokens per call -> the
   second call brings the task to 120000 >= 100000, and the task
   fails there: exact budget error, budget_exceeded in the trail
   with used/budget, NO third provider call, the second step
   never executes, no skill proposed, token_budget recorded.
K2 Under budget: a small-usage stub completes normally; the
   recorded token_budget is the default and no budget_exceeded
   event exists.
K3 Disabled: GHOST_AGENT_TOKEN_BUDGET=0 with the same fat stub
   runs to its normal end (180000 tokens) with no budget failure
   and token_budget None on the record.
K4 repo_status fallback: a non-git fixture tree (README.md,
   notes.txt, sub/deep.txt — one with a known newest mtime)
   reports the file count, the newest mtime in ISO, and a
   'carries:' list naming README.md, with NO git claims; a
   marker-less tree reports 'carries: none of the usual
   markers'; the git fixture still returns its branch and last
   commit exactly as v0.8.0.
K5 Stored output: a 5000-char tool result is stored whole in the
   task record (baseline stores 2000 and fails here).
K6 /api/system reports the configured token_budget.
K7 Back-compat: gates v1..v8 (51 tests) pass in the same run
   (subprocess, as in prior gates).

Feature assertions live inside the tests so the baseline run on
v0.8.0 code fails each case for its designed reason instead of
dying at collection.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ghost_agent.agent import Agent
from ghost_agent.models import OpenAICompatibleModel, Router
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent

FAT_USAGE = {"prompt_tokens": 40000, "completion_tokens": 20000,
             "total_tokens": 60000}


# ---- stub provider plumbing (the v7 gate's pattern) -----------------------
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
    """urlopen replacement: one scripted answer per call, and it
    records every prompt it was handed."""

    def __init__(self, script):
        self.script = script          # fn(call_no, prompt) -> payload
        self.prompts = []

    @property
    def calls(self):
        return len(self.prompts)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.prompts.append(body["messages"][0]["content"])
        return _StubResponse(self.script(len(self.prompts),
                                           self.prompts[-1]))


def _stub_agent(tmp_path, stub, monkeypatch, name="home"):
    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen", stub)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", "gate-v9-dummy-key")
    store = Store(tmp_path / name)
    router = Router(prefer_real=True)
    router.real = OpenAICompatibleModel(base_url="http://stub.invalid/v1",
                                        model="stub-loop-model")
    return store, Agent(store, router=router)


def _echo_step(text):
    return {"tool": "echo_note", "args": {"text": text},
            "verify": {"type": "ok"}}


# ---- K1 -------------------------------------------------------------------
def test_k1_budget_trips(tmp_path, monkeypatch):
    monkeypatch.delenv("GHOST_AGENT_TOKEN_BUDGET", raising=False)

    def script(n, prompt):
        # A fresh echo each call (never a loop-detect repeat);
        # usage is 60000 tokens EVERY call.
        return _step_payload(_echo_step(f"probe {n}"), FAT_USAGE)

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    task = agent.create_task("keep probing")
    assert task["token_budget"] == 100000        # the default, recorded
    assert stub.calls == 1                        # 60000 < 100000: fine
    assert (task["usage_totals"] or {})["total_tokens"] == 60000

    task = agent.run_task(task["id"])
    # The second call lands on 120000 >= 100000: the run stops
    # THERE — before the second step executes, before a third call.
    assert stub.calls == 2
    assert task["status"] == "failed"
    assert task["error"] == "token budget reached (120000 of 100000 tokens)"
    assert len(task["steps"]) == 1
    assert task["steps"][0]["status"] == "done"
    assert task["skill_id"] is None
    assert store.list("skills") == []
    events = [e for e in store.read_trail(task_id=task["id"])
              if e["type"] == "budget_exceeded"]
    assert len(events) == 1
    assert events[0]["detail"]["used"] == 120000
    assert events[0]["detail"]["budget"] == 100000
    # The usage itself is still accounted, honestly and in full.
    assert task["usage_totals"]["total_tokens"] == 120000
    assert task["usage_totals"]["calls"] == 2


# ---- K2 -------------------------------------------------------------------
def test_k2_under_budget_completes(tmp_path, monkeypatch):
    monkeypatch.delenv("GHOST_AGENT_TOKEN_BUDGET", raising=False)
    small = {"prompt_tokens": 10, "completion_tokens": 5,
             "total_tokens": 15}

    def script(n, prompt):
        if n == 1:
            return _step_payload(_echo_step("small"), small)
        return _step_payload({"tool": "done"}, small)

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    task = agent.create_task("one small step")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["token_budget"] == 100000
    assert task["usage_totals"]["total_tokens"] == 30
    assert [e for e in store.read_trail(task_id=task["id"])
            if e["type"] == "budget_exceeded"] == []


# ---- K3 -------------------------------------------------------------------
def test_k3_budget_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_AGENT_TOKEN_BUDGET", "0")

    def script(n, prompt):
        if n <= 2:
            return _step_payload(_echo_step(f"fat {n}"), FAT_USAGE)
        return _step_payload({"tool": "done"}, FAT_USAGE)

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    task = agent.create_task("spend freely, budget off")
    task = agent.run_task(task["id"])
    # 3 calls x 60000 = 180000 tokens — far over the default —
    # and the task still completes, because the budget is off.
    assert task["status"] == "done"
    assert task["token_budget"] is None
    assert task["usage_totals"]["total_tokens"] == 180000
    assert [e for e in store.read_trail(task_id=task["id"])
            if e["type"] == "budget_exceeded"] == []


# ---- K4 -------------------------------------------------------------------
def _git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(path),
         "-c", "user.email=gate@example.invalid", "-c", "user.name=Gate",
         *args],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _eco_fixture(tmp_path: Path):
    """Registry fixture: alpha-proj (a real git repo), beta-proj
    (a plain tree: README.md + notes.txt + sub/deep.txt, with
    deep.txt carrying a known newest mtime), delta-proj (a plain
    tree with none of the usual markers)."""
    repo = tmp_path / "alpha"
    repo.mkdir()
    (repo / "tracked.txt").write_text("tracked contents\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "alpha initial commit")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    plain = tmp_path / "beta"
    (plain / "sub").mkdir(parents=True)
    (plain / "README.md").write_text("# beta\n", encoding="utf-8")
    (plain / "notes.txt").write_text("beta notes\n", encoding="utf-8")
    newest = plain / "sub" / "deep.txt"
    newest.write_text("deep\n", encoding="utf-8")
    newest_ts = 1767225600  # 2026-01-01T00:00:00Z, the known newest
    for f in (plain / "README.md", plain / "notes.txt"):
        os.utime(f, (newest_ts - 86400, newest_ts - 86400))
    os.utime(newest, (newest_ts, newest_ts))

    bare = tmp_path / "delta"
    bare.mkdir()
    (bare / "random.bin.txt").write_text("delta\n", encoding="utf-8")

    store = Store(tmp_path / "home")
    fixture = [
        {"name": "alpha-proj", "repo": "littlestjames82-sys/alpha-proj",
         "local_path": str(repo), "layer": "kernel",
         "role": "fixture git project", "source": "gate fixture"},
        {"name": "beta-proj", "repo": None,
         "local_path": str(plain), "layer": "law",
         "role": "fixture plain tree", "source": "gate fixture"},
        {"name": "delta-proj", "repo": None,
         "local_path": str(bare), "layer": "tools",
         "role": "fixture marker-less tree", "source": "gate fixture"},
    ]
    (store.home / "ecosystem.json").write_text(
        json.dumps(fixture), encoding="utf-8")
    return store, branch, newest_ts


def test_k4_repo_status_fallback_facts(tmp_path):
    from ghost_agent.ecosystem import get_ecosystem
    from ghost_agent.tools import Tools

    store, branch, newest_ts = _eco_fixture(tmp_path)
    tools = Tools(store.workspace, ecosystem=get_ecosystem(store))

    res = tools.repo_status("beta-proj")
    assert res["ok"] and res["has_local"] is True
    assert res["is_git_repo"] is False
    assert res["branch"] is None and res["last_commit"] is None
    out = res["output"]
    assert "not a git repository" in out
    assert "branch" not in out            # no git data is claimed
    assert "3 file" in out                # README + notes + deep
    expected_iso = datetime.fromtimestamp(
        newest_ts, tz=timezone.utc).isoformat()
    assert expected_iso in out            # newest mtime, ISO format
    assert "carries:" in out and "README.md" in out
    assert res["file_count"] == 3
    assert res["newest_mtime"] == expected_iso
    assert "README.md" in res["markers"]

    res = tools.repo_status("delta-proj")
    assert res["ok"] and res["is_git_repo"] is False
    assert "carries: none of the usual markers" in res["output"]
    assert res["markers"] == []

    # The git path is exactly v0.8.0's answer.
    res = tools.repo_status("alpha-proj")
    assert res["ok"] and res["is_git_repo"] is True
    assert res["branch"] == branch
    assert res["last_commit"]["subject"] == "alpha initial commit"
    assert f"branch {branch}" in res["output"]


# ---- K5 -------------------------------------------------------------------
def test_k5_stored_step_output_fuller(tmp_path, monkeypatch):
    monkeypatch.delenv("GHOST_AGENT_TOKEN_BUDGET", raising=False)
    content = "0123456789" * 500          # exactly 5000 chars
    small = {"prompt_tokens": 10, "completion_tokens": 5,
             "total_tokens": 15}

    def script(n, prompt):
        if n == 1:
            return _step_payload(
                {"tool": "read_file", "args": {"path": "big.txt"},
                 "verify": {"type": "ok"}}, small)
        return _step_payload({"tool": "done"}, small)

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "big.txt").write_text(content, encoding="utf-8")
    task = agent.create_task("read big.txt")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    stored = store.get("tasks", task["id"])
    output = stored["steps"][0]["result"]["output"]
    assert len(output) > 4000             # the 2000 cap is gone
    assert output == content              # stored whole, exactly


# ---- K6 -------------------------------------------------------------------
def _start(store, agent):
    from ghost_agent.server import make_server
    server = make_server("127.0.0.1", 0, store=store, agent=agent,
                         token=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, path):
    req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_k6_system_reports_token_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("GHOST_AGENT_TOKEN_BUDGET", raising=False)
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/system")
        assert status == 200
        assert body["token_budget"] == 100000
    finally:
        server.shutdown(); server.server_close()


# ---- K7 -------------------------------------------------------------------
def test_k7_prior_gates_still_pass():
    full_env = dict(os.environ, PYTHONPATH="src",
                    PATH="/usr/bin:/bin:/usr/local/bin")
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py",
         "tests/test_sanity_gate_v5.py", "tests/test_sanity_gate_v6.py",
         "tests/test_sanity_gate_v7.py", "tests/test_sanity_gate_v7b.py",
         "tests/test_sanity_gate_v8.py",
         "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=1200)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "51 passed" in out.stdout
