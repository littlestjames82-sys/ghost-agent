"""Ghost Agent v6 SANITY GATE — known answers, run FIRST.

K1 Back-compat: v1..v5 gates (25 tests) pass in the same run
   (subprocess).
K2 Usage accounting: a stub provider response carrying usage
   {prompt 120, completion 30, total 150} driven through a real
   task run lands EXACTLY in the task totals, the trail, and
   /api/analytics (model attributed, cost unknown for an unpriced
   model); a MockModel-only run adds 0 real tokens.
K3 Import safety: export from a seeded store A -> import into B ->
   B's imported memory/skill are CANDIDATES (trusted lists empty),
   B's imported schedule exists but is DISABLED, B's imported task
   is archived + flagged imported (never in the live task list),
   duplicate ids are re-minted (B's pre-existing record survives);
   a malformed snapshot -> 400 and B's counts are unchanged;
   import without a token on a token server -> 401.
K4 search_files: exact match set with correct line numbers on a
   seeded workspace; ../ and absolute outside paths cannot escape
   the root; binary and >1MB files are skipped; results cap at 50.
K5 Append governance: an append goal plans an append_file step
   that pauses in waiting_approval; the file is byte-identical
   before approval and has exactly the appended content after.
"""
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ghost_agent.agent import Agent
from ghost_agent.memory import Memory
from ghost_agent.models import OpenAICompatibleModel, Router
from ghost_agent.scheduler import create_schedule
from ghost_agent.server import analytics_payload, export_payload, make_server
from ghost_agent.skills import Skills
from ghost_agent.store import Store
from ghost_agent.tools import ToolError, Tools

ROOT = Path(__file__).resolve().parent.parent


def _start(store, agent, token=None):
    server = make_server("127.0.0.1", 0, store=store, agent=agent, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, path, method="GET", payload=None, headers=None, raw=None):
    data = raw
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
            try:
                return r.status, json.loads(body or "{}")
            except ValueError:
                return r.status, {"_raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {}


def _mk(tmp_path, name="home"):
    store = Store(tmp_path / name)
    return store, Agent(store, router=Router(prefer_real=False))


# ---- K1 -----------------------------------------------------------------
def test_k1_prior_gates_still_pass():
    import os
    full_env = dict(os.environ, PYTHONPATH="src",
                    PATH="/usr/bin:/bin:/usr/local/bin")
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py",
         "tests/test_sanity_gate_v5.py", "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=900)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "25 passed" in out.stdout


# ---- K2 -----------------------------------------------------------------
class _StubResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_k2_usage_accounting(tmp_path, monkeypatch):
    plan = [{"tool": "echo_note", "args": {"text": "stub ran"},
             "verify": {"type": "ok"}}]
    payload = {
        "model": "stub-model-x",
        "choices": [{"message": {"content": json.dumps(plan)}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                  "total_tokens": 150},
    }
    monkeypatch.setattr(
        "ghost_agent.models.urllib.request.urlopen",
        lambda req, timeout=None: _StubResponse(payload))
    monkeypatch.setenv("GHOST_AGENT_API_KEY", "gate-v6-dummy-key")
    store = Store(tmp_path / "home")
    router = Router(prefer_real=True)
    router.real = OpenAICompatibleModel(base_url="http://stub.invalid/v1",
                                        model="stub-model-x")
    agent = Agent(store, router=router)
    task = agent.create_task("echo the stub plan")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    totals = task["usage_totals"]
    assert totals["prompt_tokens"] == 120
    assert totals["completion_tokens"] == 30
    assert totals["total_tokens"] == 150
    assert task["usage_by_model"]["stub-model-x"]["total_tokens"] == 150
    events = [e for e in store.read_trail(task_id=task["id"])
              if e["type"] == "model_usage"]
    assert len(events) == 1
    assert events[0]["detail"]["total_tokens"] == 150
    assert events[0]["detail"]["source"] == "provider"
    an = analytics_payload(store, agent)
    usage = an["usage"]
    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 30
    assert usage["total_tokens"] == 150
    assert usage["by_model"]["stub-model-x"]["prompt_tokens"] == 120
    # stub-model-x is not in the price table: cost is unknown, never guessed.
    assert usage["estimated_cost_usd"] is None
    assert usage["cost_status"] == "unknown"

    # A MockModel-only run records usage 0 with source "mock".
    store2, agent2 = _mk(tmp_path, "mockhome")
    t2 = agent2.create_task("read nothing-here.txt")
    agent2.run_task(t2["id"])
    t2 = store2.get("tasks", t2["id"])
    assert t2["usage_totals"]["total_tokens"] == 0
    an2 = analytics_payload(store2, agent2)
    assert an2["usage"]["total_tokens"] == 0
    assert an2["usage"]["mock_calls"] == 1
    assert an2["usage"]["provider_calls"] == 0


# ---- K3 -----------------------------------------------------------------
def _seed_source(tmp_path):
    store, agent = _mk(tmp_path, "a")
    mem = Memory(store)
    cand = mem.propose("Imported trusted fact", source="human")
    mem.approve(cand["id"])                       # trusted in A
    task = agent.create_task("demo")
    agent.run_task(task["id"])
    task = agent.approve_task(task["id"])         # done in A
    Skills(store).promote(task["skill_id"])       # trusted skill in A
    create_schedule(store, "read demo_output.txt", kind="interval",
                    interval_minutes=60)          # enabled in A
    return store, cand["id"], export_payload(store)


def test_k3_import_safety(tmp_path):
    _, src_mem_id, snapshot = _seed_source(tmp_path)
    store_b, agent_b = _mk(tmp_path, "b")
    # Pre-existing record in B with the SAME id as A's trusted memory.
    store_b.put("memories", {
        "id": src_mem_id, "text": "pre-existing local memory",
        "status": "candidate", "source": "local", "verified": False,
        "superseded_by": None})
    server, base = _start(store_b, agent_b)
    try:
        status, body = _req(base, "/api/import", method="POST",
                            payload=snapshot)
        assert status == 200, body
        counts = body["imported"]
        assert counts["memories"] == 1 and counts["skills"] == 1
        assert counts["schedules"] == 1 and counts["tasks"] >= 1

        mem_b = Memory(store_b)
        assert mem_b.list_trusted() == []                 # never trusted
        cands = {m["text"]: m for m in mem_b.list_candidates()}
        assert "pre-existing local memory" in cands       # not overwritten
        imported = cands["Imported trusted fact"]
        assert imported["id"] != src_mem_id               # re-minted
        assert imported["source"].startswith("import:")
        assert imported["verified"] is False

        skills_b = Skills(store_b).list_all()
        assert skills_b and all(
            s["status"] == "candidate" and s["trusted"] is False
            for s in skills_b)

        scheds = store_b.list("schedules")
        assert len(scheds) == 1 and scheds[0]["enabled"] is False

        archived = store_b.list("archived_tasks")
        assert archived and all(t.get("imported") is True for t in archived)
        assert store_b.list("tasks") == []                # never live

        def coll_counts():
            return {name: len(store_b.list(name)) for name in
                    ("memories", "skills", "schedules", "tasks",
                     "archived_tasks", "archived_approvals",
                     "archived_trail")}
        before = coll_counts()
        status, _ = _req(base, "/api/import", method="POST",
                         payload={"bogus": True})
        assert status == 400
        status, _ = _req(base, "/api/import", method="POST",
                         raw=b"{not json")
        assert status == 400
        assert coll_counts() == before                    # NOTHING changed
    finally:
        server.shutdown(); server.server_close()

    # Token gate applies to import exactly like every other /api route.
    store_c, agent_c = _mk(tmp_path, "c")
    server, base = _start(store_c, agent_c, token="gate-v6-secret")
    try:
        status, _ = _req(base, "/api/import", method="POST",
                         payload=snapshot)
        assert status == 401
        status, _ = _req(base, "/api/import", method="POST",
                         payload=snapshot,
                         headers={"X-Ghost-Token": "gate-v6-secret"})
        assert status == 200
    finally:
        server.shutdown(); server.server_close()


# ---- K4 -----------------------------------------------------------------
def test_k4_search_files(tmp_path):
    store, _ = _mk(tmp_path)
    ws = store.workspace
    (ws / "docs").mkdir()
    (ws / "docs" / "a.txt").write_text(
        "alpha\nbeta needle\ngamma needle\n", encoding="utf-8")
    (ws / "b.log").write_text("needle here\n", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "c.txt").write_text("nothing to see\n", encoding="utf-8")
    (ws / "bin.dat").write_bytes(b"\x00\x01needle\n")
    (ws / "big.txt").write_text("needle " + "x" * 1_100_000,
                                encoding="utf-8")
    (tmp_path / "outside.txt").write_text("needle outside\n",
                                          encoding="utf-8")
    tools = Tools(ws)
    res = tools.search_files("needle")
    assert res["ok"] is True
    got = {(m["path"], m["line"]) for m in res["matches"]}
    assert got == {("docs/a.txt", 2), ("docs/a.txt", 3), ("b.log", 1)}
    by_key = {(m["path"], m["line"]): m["text"] for m in res["matches"]}
    assert by_key[("docs/a.txt", 2)] == "beta needle"
    # Scoped search stays in scope.
    res_docs = tools.search_files("needle", path="docs")
    assert {(m["path"], m["line"]) for m in res_docs["matches"]} == \
        {("docs/a.txt", 2), ("docs/a.txt", 3)}
    # Traversal cannot escape the root.
    for bad in ("../", str(tmp_path)):
        try:
            out = tools.search_files("needle", path=bad)
            paths = {m["path"] for m in out.get("matches", [])}
            assert "outside.txt" not in paths
            assert all(".." not in p.split("/") for p in paths)
        except ToolError:
            pass  # refusing outright is also correct

    # Result cap: 60 matching files -> at most 50 matches.
    store2, _ = _mk(tmp_path, "caphome")
    for i in range(60):
        (store2.workspace / f"f{i:02d}.txt").write_text(
            "needle\n", encoding="utf-8")
    res2 = Tools(store2.workspace).search_files("needle")
    assert len(res2["matches"]) == 50


# ---- K5 -----------------------------------------------------------------
def test_k5_append_governance(tmp_path):
    store, agent = _mk(tmp_path)
    target = store.workspace / "notes.txt"
    target.write_text("line one\n", encoding="utf-8")
    before = target.read_bytes()
    task = agent.create_task("append line two to notes.txt")
    assert [s["tool"] for s in task["steps"]] == ["append_file"]
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    assert target.read_bytes() == before              # untouched pre-approval
    task = agent.approve_task(task["id"])
    assert task["status"] == "done"
    assert target.read_bytes() == b"line one\nline two"
