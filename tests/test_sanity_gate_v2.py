"""Ghost Agent v2 SANITY GATE — known answers, run FIRST.

K1 Back-compat: GET /api/board keeps the v1 shape on seeded data.
K2 Fail-closed: serve on a non-loopback host without a dashboard token
   refuses to start (non-zero exit, nothing served).
K3 Token gate: with GHOST_AGENT_DASHBOARD_TOKEN set, /api/board without
   the token -> 401; with the token (header) -> 200.
K4 Trail search: /api/trail?q=<marker> returns only matching events;
   q=<absent> returns empty.
K5 Memory action: candidate proposed via the store, POST approve over
   HTTP -> trusted; unknown id -> 404.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ghost_agent.agent import Agent
from ghost_agent.memory import Memory
from ghost_agent.models import Router
from ghost_agent.server import make_server
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent


def _start(store, agent):
    server = make_server("127.0.0.1", 0, store=store, agent=agent)
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
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {}


@pytest.fixture()
def seeded(tmp_path):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    task = agent.create_task("write gate.txt with: gate-content")
    agent.run_task(task["id"])  # -> waiting_approval
    return store, agent, task


# ---- K1 -----------------------------------------------------------------
def test_k1_board_back_compat(seeded):
    store, agent, task = seeded
    server, base = _start(store, agent)
    try:
        status, board = _req(base, "/api/board")
        assert status == 200
        assert set(board.keys()) == {"columns", "pending_approvals"}
        assert set(board["columns"]) == {"running", "waiting_approval",
                                         "done", "failed"}
        assert [t["id"] for t in board["columns"]["waiting_approval"]] == [task["id"]]
        assert len(board["pending_approvals"]) == 1
    finally:
        server.shutdown(); server.server_close()


# ---- K2 -----------------------------------------------------------------
def test_k2_fail_closed_non_loopback_without_token(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k != "GHOST_AGENT_DASHBOARD_TOKEN"}
    env["GHOST_AGENT_HOME"] = str(tmp_path / "home")
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ghost_agent", "serve",
         "--host", "0.0.0.0", "--port", "8891"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("server kept running on a non-loopback host without "
                    "a token — fail-closed gate did NOT refuse")
    assert proc.returncode != 0
    assert "token" in out.lower()


# ---- K3 -----------------------------------------------------------------
def test_k3_token_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_AGENT_DASHBOARD_TOKEN", "gate-secret-123")
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    server, base = _start(store, agent)
    try:
        status, _ = _req(base, "/api/board")
        assert status == 401
        status, board = _req(base, "/api/board",
                             headers={"X-Ghost-Token": "gate-secret-123"})
        assert status == 200 and "columns" in board
        status, _ = _req(base, "/api/board", headers={"X-Ghost-Token": "wrong"})
        assert status == 401
        # cookie path used by the page's token form
        status, _ = _req(base, "/api/board",
                         headers={"Cookie": "ghost_token=gate-secret-123"})
        assert status == 200
        # the page itself still loads so a human can enter the token
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200 and b"Ghost Agent" in r.read()
    finally:
        server.shutdown(); server.server_close()


# ---- K4 -----------------------------------------------------------------
def test_k4_trail_search(seeded):
    store, agent, task = seeded
    store.append_trail("note", {"marker": "UNIQUE-MARKER-7f3a"}, task_id=task["id"])
    store.append_trail("note", {"marker": "plain-other"}, task_id=task["id"])
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/trail?q=UNIQUE-MARKER-7f3a")
        assert status == 200
        hits = body["trail"]
        assert hits, "marker search returned nothing"
        assert all("UNIQUE-MARKER-7f3a" in json.dumps(e) for e in hits)
        status, body = _req(base, "/api/trail?q=ABSENT-STRING-9z9z")
        assert status == 200 and body["trail"] == []
        status, body = _req(base, "/api/trail?type=note")
        assert status == 200
        assert body["trail"] and all(e["type"] == "note" for e in body["trail"])
    finally:
        server.shutdown(); server.server_close()


# ---- K5 -----------------------------------------------------------------
def test_k5_memory_approve_over_http(tmp_path):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    cand = Memory(store).propose("gate memory fact", source="gate")
    server, base = _start(store, agent)
    try:
        status, _ = _req(base, "/api/memory/mem_doesnotexist/approve",
                         method="POST", payload={})
        assert status == 404
        status, body = _req(base, f"/api/memory/{cand['id']}/approve",
                            method="POST", payload={})
        assert status == 200
        _, mem = _req(base, "/api/memory")
        assert [m["id"] for m in mem["trusted"]] == [cand["id"]]
        assert mem["candidates"] == []
    finally:
        server.shutdown(); server.server_close()
