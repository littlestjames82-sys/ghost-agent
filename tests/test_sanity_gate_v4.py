"""Ghost Agent v4 SANITY GATE — known answers, run FIRST.

K1 Scheduler fires once: an interval schedule due at injected now
   fires exactly once in tick(); a second tick at the same now fires
   zero; a disabled due schedule fires zero; missed intervals are
   skipped (never burst-fires).
K2 Scheduler respects governance: a due schedule with a write goal
   creates a task that ends waiting_approval and the target file
   does NOT exist.
K3 Bulk decide: seed 2 pending approvals; POST
   /api/approvals/decide_all {decision:"approved"} -> both files
   land with exact content, response decided=2, trail contains 2
   approval_granted events; a bad decision value -> 400.
K4 Search grouping: a unique marker planted in a task title, a
   memory text, a skill name, and a trail event ->
   GET /api/search?q=<marker> returns all four groups non-empty
   containing the planted items; q=<absent> -> all groups empty.
K5 Retry + export hygiene: retry a failed task -> new id, same
   goal, original unchanged, retrying a non-failed task -> 400;
   GET /api/export has all snapshot keys and contains neither the
   dashboard token value nor the API key value when both are set
   in env.
K6 Back-compat under v4: the v1+v2+v3 gates (14 tests) still pass
   in the same run (subprocess).
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

from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.scheduler import create_schedule, tick
from ghost_agent.server import make_server
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENV = "GHOST_AGENT_DASHBOARD_TOKEN"
UTC = timezone.utc


def _start(store, agent, token=None):
    server = make_server("127.0.0.1", 0, store=store, agent=agent, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, path, method="GET", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw or "{}")
            except ValueError:
                return r.status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {}


def _mk(tmp_path):
    store = Store(tmp_path / "home")
    return store, Agent(store, router=Router(prefer_real=False))


# ---- K1 -----------------------------------------------------------------
def test_k1_scheduler_fires_once(tmp_path):
    store, agent = _mk(tmp_path)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    s = create_schedule(store, "read x.txt", kind="interval",
                        interval_minutes=30, now=t0)
    assert s["next_run_at"] == datetime(2026, 1, 1, 12, 30, tzinfo=UTC).isoformat()
    # not yet due
    assert tick(store, agent, now=t0) == []
    assert tick(store, agent, now=datetime(2026, 1, 1, 12, 29, tzinfo=UTC)) == []
    # due: fires exactly once
    fired = tick(store, agent, now=datetime(2026, 1, 1, 12, 30, tzinfo=UTC))
    assert fired == [s["id"]]
    after = store.get("schedules", s["id"])
    assert after["last_task_id"]
    assert after["last_run_at"] == datetime(2026, 1, 1, 12, 30, tzinfo=UTC).isoformat()
    assert after["next_run_at"] == datetime(2026, 1, 1, 13, 0, tzinfo=UTC).isoformat()
    # second tick at the same instant fires zero
    assert tick(store, agent, now=datetime(2026, 1, 1, 12, 30, tzinfo=UTC)) == []
    # hours later: fires ONCE (missed intervals skipped, no burst)
    fired = tick(store, agent, now=datetime(2026, 1, 1, 15, 0, tzinfo=UTC))
    assert fired == [s["id"]]
    after = store.get("schedules", s["id"])
    assert after["next_run_at"] == datetime(2026, 1, 1, 15, 30, tzinfo=UTC).isoformat()
    # a disabled due schedule fires zero
    s2 = create_schedule(store, "read y.txt", kind="interval",
                         interval_minutes=5, now=t0)
    store.update("schedules", s2["id"], enabled=False)
    fired = tick(store, agent, now=datetime(2026, 1, 1, 16, 0, tzinfo=UTC))
    assert s2["id"] not in fired
    assert store.get("schedules", s2["id"])["last_task_id"] is None


# ---- K2 -----------------------------------------------------------------
def test_k2_scheduler_respects_governance(tmp_path):
    store, agent = _mk(tmp_path)
    t0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    s = create_schedule(store, "write sched-gate.txt with: hello",
                        kind="interval", interval_minutes=10, now=t0)
    fired = tick(store, agent, now=datetime(2026, 1, 1, 9, 10, tzinfo=UTC))
    assert fired == [s["id"]]
    task_id = store.get("schedules", s["id"])["last_task_id"]
    task = store.get("tasks", task_id)
    assert task["status"] == "waiting_approval"   # governance held
    assert not (store.workspace / "sched-gate.txt").exists()  # nothing executed
    pend = agent.pending_approvals()
    assert len(pend) == 1 and pend[0]["task_id"] == task_id


# ---- K3 -----------------------------------------------------------------
def test_k3_bulk_decide(tmp_path):
    store, agent = _mk(tmp_path)
    ta = agent.create_task("write bulk-a.txt with: alpha")
    agent.run_task(ta["id"])
    tb = agent.create_task("write bulk-b.txt with: beta")
    agent.run_task(tb["id"])
    assert len(agent.pending_approvals()) == 2
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/approvals/decide_all", method="POST",
                            payload={"decision": "maybe"})
        assert status == 400
        assert len(agent.pending_approvals()) == 2   # nothing decided
        status, body = _req(base, "/api/approvals/decide_all", method="POST",
                            payload={"decision": "approved"})
        assert status == 200
        assert body["decided"] == 2
        results = {r["approval_id"]: r for r in body["results"]}
        assert len(results) == 2
        assert all(r["ok"] and r["status"] == "approved"
                   for r in results.values())
        # both files landed with exact content, through the per-item path
        assert (store.workspace / "bulk-a.txt").read_text(encoding="utf-8") == "alpha"
        assert (store.workspace / "bulk-b.txt").read_text(encoding="utf-8") == "beta"
        granted = [e for e in store.read_trail(limit=1_000_000)
                   if e["type"] == "approval_granted"]
        assert len(granted) == 2
        # nothing left pending; a second bulk call decides zero
        status, body = _req(base, "/api/approvals/decide_all", method="POST",
                            payload={"decision": "approved"})
        assert status == 200 and body["decided"] == 0
    finally:
        server.shutdown(); server.server_close()


# ---- K4 -----------------------------------------------------------------
def test_k4_search_grouping(tmp_path):
    store, agent = _mk(tmp_path)
    marker = "zqxmarker"
    task = agent.create_task("read x.txt", title=f"Task {marker}")
    from ghost_agent.memory import Memory
    mem = Memory(store).propose(f"remember the {marker} fact", source="gate")
    skill = store.put("skills", {"id": "skl_gate", "name": f"skill-{marker}",
                                 "status": "candidate", "trusted": False,
                                 "from_task": None, "steps": [], "tools": [],
                                 "permissions": []})
    ev = store.append_trail("note", {"marker": marker})
    server, base = _start(store, agent)
    try:
        status, body = _req(base, f"/api/search?q={marker}")
        assert status == 200
        assert [t["id"] for t in body["tasks"]] == [task["id"]]
        assert [m["id"] for m in body["memories"]] == [mem["id"]]
        assert [s["id"] for s in body["skills"]] == [skill["id"]]
        assert ev["id"] in [e["id"] for e in body["trail"]]
        status, body = _req(base, "/api/search?q=absent-zqx-zzz")
        assert status == 200
        assert body == {"tasks": [], "memories": [], "skills": [], "trail": []}
        status, body = _req(base, "/api/search")   # no q at all
        assert status == 200
        assert body == {"tasks": [], "memories": [], "skills": [], "trail": []}
    finally:
        server.shutdown(); server.server_close()


# ---- K5 -----------------------------------------------------------------
def test_k5_retry_and_export_hygiene(tmp_path, monkeypatch):
    token_value = "gate4-token-SECRET-111"
    key_value = "sk-gate4-SECRET-222"
    monkeypatch.setenv(TOKEN_ENV, token_value)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", key_value)
    store, agent = _mk(tmp_path)
    task = agent.create_task("write retry-gate.txt with: data")
    agent.run_task(task["id"])
    agent.deny_task(task["id"])                    # -> failed
    orig = store.get("tasks", task["id"])
    assert orig["status"] == "failed"
    server, base = _start(store, agent)            # token picked up from env
    hdr = {"X-Ghost-Token": token_value}
    try:
        status, _ = _req(base, "/api/export")
        assert status == 401                       # token still gates export
        status, body = _req(base, "/api/export", headers=hdr)
        assert status == 200
        for key in ("version", "exported_at", "tasks", "approvals",
                    "memories_trusted", "memories_candidates", "skills",
                    "schedules", "trail"):
            assert key in body, key
        blob = json.dumps(body)
        assert token_value not in blob
        assert key_value not in blob
        # retry the failed task
        status, body = _req(base, f"/api/tasks/{task['id']}/retry",
                            method="POST", headers=hdr)
        assert status in (200, 201)
        new_task = body["task"]
        assert new_task["id"] != task["id"]
        assert new_task["goal"] == orig["goal"]
        assert new_task["title"].endswith("(retry)")
        assert new_task["status"] == "waiting_approval"  # write still gated
        # original untouched
        after = store.get("tasks", task["id"])
        assert after["status"] == "failed"
        assert after["steps"] == orig["steps"]
        assert after["title"] == orig["title"]
        # retrying a non-failed task -> 400
        status, body = _req(base, f"/api/tasks/{new_task['id']}/retry",
                            method="POST", headers=hdr)
        assert status == 400
        assert "error" in body
    finally:
        server.shutdown(); server.server_close()


# ---- K6 -----------------------------------------------------------------
def test_k6_prior_gates_still_pass():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop(TOKEN_ENV, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "-q"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "14 passed" in proc.stdout
