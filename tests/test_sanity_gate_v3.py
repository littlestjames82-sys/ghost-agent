"""Ghost Agent v3 SANITY GATE — known answers, run FIRST.

K1 Approvals history: seed one granted, one denied, one pending
   approval; GET /api/approvals returns all 3 with correct statuses
   and task titles, newest first.
K2 Propose is candidate-only: POST /api/memory/propose creates a
   memory that appears in candidates and NOT in trusted; empty
   text -> 400.
K3 Activity integrity: on seeded data, sum(activity_by_day counts)
   in /api/analytics equals trail_total, and the per-day counts are
   the seeded events' actual dates.
K4 System health + hygiene: /api/system health reports
   workspace_writable true on a temp home, and the serialized
   payload contains neither the dashboard token value nor any API
   key value when both are set in env.
K5 Back-compat under v3: the v1 gate (4/4) and v2 gate (5/5) still
   pass in the same run (subprocess, 9 passed).
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.server import make_server
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENV = "GHOST_AGENT_DASHBOARD_TOKEN"


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


def _seed_three_approvals(store, agent):
    t1 = agent.create_task("write gate-a.txt with: aaa")
    agent.run_task(t1["id"])
    agent.approve_task(t1["id"])          # granted -> task done
    t2 = agent.create_task("write gate-b.txt with: bbb")
    agent.run_task(t2["id"])
    agent.deny_task(t2["id"])             # denied -> task failed
    t3 = agent.create_task("write gate-c.txt with: ccc")
    agent.run_task(t3["id"])              # pending -> waiting_approval
    return t1, t2, t3


# ---- K1 -----------------------------------------------------------------
def test_k1_approvals_history(tmp_path):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    t1, t2, t3 = _seed_three_approvals(store, agent)
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/approvals")
        assert status == 200
        items = body["approvals"]
        assert len(items) == 3
        by_task = {a["task_id"]: a for a in items}
        assert by_task[t1["id"]]["status"] == "approved"
        assert by_task[t2["id"]]["status"] == "denied"
        assert by_task[t3["id"]]["status"] == "pending"
        for t in (t1, t2, t3):
            assert by_task[t["id"]]["task_title"] == t["title"]
            assert by_task[t["id"]]["tool"] == "write_file"
        # newest first: pending (created last) first, granted (first) last
        assert items[0]["task_id"] == t3["id"]
        assert items[-1]["task_id"] == t1["id"]
        created = [a["created_at"] for a in items]
        assert created == sorted(created, reverse=True)
    finally:
        server.shutdown(); server.server_close()


# ---- K2 -----------------------------------------------------------------
def test_k2_propose_is_candidate_only(tmp_path):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/memory/propose", method="POST",
                            payload={"text": "gate proposed fact",
                                     "source": "gate"})
        assert status in (200, 201)
        mem_id = body["memory"]["id"]
        assert body["memory"]["status"] == "candidate"
        assert body["memory"]["verified"] is False
        status, mem = _req(base, "/api/memory")
        assert status == 200
        assert [m["id"] for m in mem["candidates"]] == [mem_id]
        assert mem["trusted"] == []
        # empty / whitespace / missing text -> 400, nothing created
        for bad in ({"text": ""}, {"text": "   "}, {}):
            status, _ = _req(base, "/api/memory/propose", method="POST",
                             payload=bad)
            assert status == 400
        _, mem = _req(base, "/api/memory")
        assert len(mem["candidates"]) == 1 and mem["trusted"] == []
    finally:
        server.shutdown(); server.server_close()


# ---- K3 -----------------------------------------------------------------
def test_k3_activity_integrity(tmp_path):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    task = agent.create_task("demo")
    agent.run_task(task["id"])
    agent.approve_task(task["id"])        # a burst of events, all today
    # one raw event backdated 5 days, appended straight to the trail file
    old_day = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
    raw = {"id": "ev_backdated", "ts": f"{old_day}T12:00:00+00:00",
           "ts_ns": 1, "type": "note", "task_id": None,
           "step_index": None, "tool": None, "detail": {"backdated": True}}
    with open(store.trail_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(raw, sort_keys=True) + "\n")
    # expected per-day counts from the events' own timestamps
    events = store.read_trail(limit=1_000_000)
    expected = {}
    for e in events:
        expected[e["ts"][:10]] = expected.get(e["ts"][:10], 0) + 1
    server, base = _start(store, agent)
    try:
        status, an = _req(base, "/api/analytics")
        assert status == 200
        days = an["activity_by_day"]
        assert len(days) == 14
        assert sum(d["count"] for d in days) == an["trail_total"]
        assert an["trail_total"] == len(events)
        got = {d["date"]: d["count"] for d in days if d["count"]}
        assert got == expected
        # window ends today (UTC) and is consecutive
        dates = [d["date"] for d in days]
        assert dates[-1] == datetime.now(timezone.utc).date().isoformat()
        parsed = [datetime.fromisoformat(d).date() for d in dates]
        assert all((b - a).days == 1 for a, b in zip(parsed, parsed[1:]))
        # overview carries the same series
        status, ov = _req(base, "/api/overview")
        assert status == 200
        assert ov["activity_by_day"] == days
    finally:
        server.shutdown(); server.server_close()


# ---- K4 -----------------------------------------------------------------
def test_k4_system_health_and_hygiene(tmp_path, monkeypatch):
    token_value = "gate-token-SECRET-456"
    key_value = "sk-gate-SECRET-789"
    monkeypatch.setenv(TOKEN_ENV, token_value)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", key_value)
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    task = agent.create_task("read x.txt")
    server, base = _start(store, agent)   # token picked up from env
    try:
        status, _ = _req(base, "/api/system")
        assert status == 401              # token still gates the endpoint
        status, sysp = _req(base, "/api/system",
                            headers={"X-Ghost-Token": token_value})
        assert status == 200
        health = sysp["health"]
        assert health["workspace_writable"] is True
        assert health["store_home_exists"] is True
        assert health["workspace_exists"] is True
        assert health["python_version"]
        assert health["tasks_total"] == 1
        assert health["trail_events_total"] == len(
            store.read_trail(limit=1_000_000))
        assert health["server_time_utc"]
        blob = json.dumps(sysp)
        assert token_value not in blob
        assert key_value not in blob
        assert sysp["api_key_present"] is True
        assert sysp["auth"]["token_required"] is True
    finally:
        server.shutdown(); server.server_close()


# ---- K5 -----------------------------------------------------------------
def test_k5_prior_gates_still_pass():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop(TOKEN_ENV, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py", "-q"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "9 passed" in proc.stdout
