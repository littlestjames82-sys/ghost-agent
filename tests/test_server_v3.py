"""Ghost Agent v3 dashboard: approvals history endpoint, memory
propose endpoint, activity series, system health, page landmarks."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.server import (activity_by_day, approvals_payload,
                                make_server, system_payload)
from ghost_agent.store import Store

TOKEN_ENV = "GHOST_AGENT_DASHBOARD_TOKEN"


@pytest.fixture()
def running_server(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    server = make_server("127.0.0.1", 0, store=store, agent=agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, store, agent
    server.shutdown()
    server.server_close()


def _req(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {}


def test_approvals_payload_empty(store):
    agent = Agent(store, router=Router(prefer_real=False))
    assert approvals_payload(store, agent) == []


def test_approvals_endpoint_empty(running_server):
    base, _, _ = running_server
    status, body = _req(base, "/api/approvals")
    assert status == 200 and body == {"approvals": []}


def test_propose_default_source_and_candidate(running_server):
    base, store, _ = running_server
    status, body = _req(base, "/api/memory/propose", method="POST",
                        payload={"text": "no source given"})
    assert status == 201
    assert body["memory"]["source"] == "human"
    assert body["memory"]["status"] == "candidate"
    status, body = _req(base, "/api/memory/propose", method="POST",
                        payload={"text": "with source", "source": "ryan"})
    assert status == 201 and body["memory"]["source"] == "ryan"
    _, mem = _req(base, "/api/memory")
    assert len(mem["candidates"]) == 2 and mem["trusted"] == []


def test_activity_zero_fill_empty_store(store):
    days = activity_by_day(store)
    assert len(days) == 14
    assert all(d["count"] == 0 for d in days)
    dates = [d["date"] for d in days]
    assert dates == sorted(dates)


def test_activity_counts_today(store):
    store.append_trail("note", {"n": 1})
    store.append_trail("note", {"n": 2})
    days = activity_by_day(store)
    assert sum(d["count"] for d in days) == 2
    assert days[-1]["count"] == 2


def test_system_health_block(store):
    agent = Agent(store, router=Router(prefer_real=False))
    task = agent.create_task("read x.txt")
    agent.run_task(task["id"])
    sysp = system_payload(store, agent, None, "127.0.0.1")
    health = sysp["health"]
    assert health["store_home_exists"] is True
    assert health["workspace_exists"] is True
    assert health["workspace_writable"] is True
    assert health["tasks_total"] == 1
    assert health["trail_events_total"] > 0
    assert health["python_version"]
    assert health["server_time_utc"].endswith("+00:00")


def test_health_probe_leaves_no_files(store):
    agent = Agent(store, router=Router(prefer_real=False))
    system_payload(store, agent, None, "127.0.0.1")
    leftovers = [p.name for p in store.workspace.iterdir()
                 if p.name.startswith(".health-")]
    assert leftovers == []


def test_system_endpoint_has_health(running_server):
    base, _, _ = running_server
    status, sysp = _req(base, "/api/system")
    assert status == 200
    assert sysp["health"]["workspace_writable"] is True


def test_v3_page_landmarks(running_server):
    base, _, _ = running_server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        html = r.read().decode()
    # approvals history
    assert "Recent decisions" in html and 'id="ap-decisions"' in html
    # memory propose form
    assert 'id="mem-form"' in html and "Add a memory" in html
    assert "/api/memory/propose" in html
    # logs pause + severity + download
    assert 'id="log-pause"' in html and "Pause logs" in html
    assert 'id="log-sev"' in html and "Download JSON" in html
    # analytics donut container
    assert "an-donut" in html and "conic-gradient" in html
    # system health + posture + serve commands
    assert "sys-health" in html and "Auth posture" in html
    assert "cmd-local" in html and "cmd-token" in html
    # board filter, task filters, skill filters, overview activity
    assert 'id="board-q"' in html and 'id="task-filters"' in html
    assert 'id="skill-filters"' in html and 'id="ov-activity"' in html
    # sidebar live-count badges
    assert 'id="nav-tasks"' in html and 'id="nav-memory"' in html
    assert 'id="nav-approvals"' in html
    # task templates + feedback lines + confirm-on-deny
    assert "fillGoal('demo')" in html and "confirm(" in html
    assert 'id="ap-status"' in html and 'id="mem-status"' in html
    # v2 invariants still hold
    assert "setInterval(refresh,5000)" in html
    assert "NO AUTHENTICATION" in html
