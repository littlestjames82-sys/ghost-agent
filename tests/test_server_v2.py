"""Ghost Agent v2 dashboard: new endpoints, auth config, page content."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from ghost_agent import __version__
from ghost_agent.agent import Agent
from ghost_agent.memory import Memory
from ghost_agent.models import Router
from ghost_agent.server import (DashboardConfigError, analytics_payload,
                                make_server, overview_payload, serve,
                                system_payload)
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


def _seed_two_tasks(store, agent):
    t1 = agent.create_task("write a.txt with: one")
    agent.run_task(t1["id"])
    agent.approve_task(t1["id"])                      # -> done
    t2 = agent.create_task("write b.txt with: two")
    agent.run_task(t2["id"])
    agent.deny_task(t2["id"])                         # -> failed
    t3 = agent.create_task("write c.txt with: three")
    agent.run_task(t3["id"])                          # -> waiting_approval
    return t1, t2, t3


def test_overview_payload(store):
    agent = Agent(store, router=Router(prefer_real=False))
    t1, t2, t3 = _seed_two_tasks(store, agent)
    ov = overview_payload(store, agent)
    assert ov["counts"] == {"total": 3, "running": 0, "waiting_approval": 1,
                            "done": 1, "failed": 1}
    assert ov["version"] == __version__
    assert [r["id"] for r in ov["recent_tasks"]] == [t3["id"], t2["id"], t1["id"]]
    assert ov["recent_tasks"][0]["steps_total"] >= 1
    assert len(ov["pending_approvals"]) == 1
    pend = ov["pending_approvals"][0]
    assert pend["task_id"] == t3["id"] and pend["task_title"] == t3["title"]
    assert pend["tool"] == "write_file"
    assert ov["trail_events"] > 0


def test_overview_endpoint(running_server):
    base, store, agent = running_server
    _seed_two_tasks(store, agent)
    status, ov = _req(base, "/api/overview")
    assert status == 200 and ov["counts"]["total"] == 3


def test_analytics_payload(store):
    agent = Agent(store, router=Router(prefer_real=False))
    _seed_two_tasks(store, agent)
    an = analytics_payload(store, agent)
    assert an["tasks_by_status"] == {"done": 1, "failed": 1, "waiting_approval": 1}
    assert an["tasks_by_model"] == {"mock": 3}
    assert an["steps_executed"] == 1 and an["steps_total"] == 3
    assert an["approvals_granted"] == 1 and an["approvals_denied"] == 1
    assert an["approvals_pending"] == 1
    assert an["tokens_tracked"] is False
    assert an["tokens_note"] == "token/cost not tracked yet"
    assert an["trail_by_type"].get("tool_execute") == 1


def test_trail_newest_first_and_limit(running_server):
    base, store, agent = running_server
    store.append_trail("note", {"n": 1})
    store.append_trail("note", {"n": 2})
    store.append_trail("note", {"n": 3})
    _, body = _req(base, "/api/trail")
    assert [e["detail"]["n"] for e in body["trail"]] == [3, 2, 1]
    _, body = _req(base, "/api/trail?limit=2")
    assert [e["detail"]["n"] for e in body["trail"]] == [3, 2]


def test_memory_reject_and_skill_promote_endpoints(running_server):
    base, store, agent = running_server
    cand = Memory(store).propose("reject me", source="test")
    status, _ = _req(base, f"/api/memory/{cand['id']}/reject",
                     method="POST", payload={})
    assert status == 200
    _, mem = _req(base, "/api/memory")
    assert mem["trusted"] == [] and mem["candidates"] == []

    task = agent.create_task("demo")
    agent.run_task(task["id"])
    agent.approve_task(task["id"])
    _, skills = _req(base, "/api/skills")
    skill = skills["skills"][0]
    assert skill["trusted"] is False
    status, body = _req(base, f"/api/skills/{skill['id']}/promote",
                        method="POST", payload={})
    assert status == 200 and body["skill"]["trusted"] is True
    status, _ = _req(base, "/api/skills/skl_nope/promote",
                     method="POST", payload={})
    assert status == 404


def test_system_payload_no_secrets(store, monkeypatch):
    agent = Agent(store, router=Router(prefer_real=False))
    monkeypatch.delenv("GHOST_AGENT_API_KEY", raising=False)
    sysp = system_payload(store, agent, None, "127.0.0.1")
    assert sysp["version"] == __version__
    assert sysp["home"] == str(store.home)
    assert sysp["workspace"] == str(store.workspace)
    assert sysp["model_adapter"] == "mock"
    assert sysp["api_key_present"] is False
    assert sysp["auth"]["token_required"] is False

    monkeypatch.setenv("GHOST_AGENT_API_KEY", "sk-DO-NOT-LEAK-123")
    sysp = system_payload(store, agent, "tok-SECRET-VALUE-789", "127.0.0.1")
    assert sysp["api_key_present"] is True
    assert "sk-DO-NOT-LEAK-123" not in json.dumps(sysp)
    # the token VALUE is never in the payload (field names like
    # token_required / token_env are fine and expected)
    assert "tok-SECRET-VALUE-789" not in json.dumps(sysp)
    assert sysp["auth"]["token_required"] is True


def test_system_endpoint(running_server):
    base, _, _ = running_server
    status, sysp = _req(base, "/api/system")
    assert status == 200 and sysp["version"] == __version__


def test_serve_fail_closed_unit(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(DashboardConfigError):
        serve("0.0.0.0", 0)


def test_non_loopback_with_token_starts(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "tok-abc")
    server = make_server("0.0.0.0", 0)
    try:
        assert server.server_address[1] > 0
    finally:
        server.server_close()


def test_v2_page_structure(running_server):
    base, _, _ = running_server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        html = r.read().decode()
    for label in ["Overview", "Board", "Approvals", "Tasks", "Memory",
                  "Skills", "Analytics", "Logs", "System"]:
        assert label in html
    for page in ["overview", "board", "approvals", "tasks", "memory",
                 "skills", "analytics", "logs", "system"]:
        assert f'id="page-{page}"' in html
    assert "sidebar" in html
    assert "NO AUTHENTICATION" in html          # v1 warning kept (no token here)
    assert 'id="loginbox"' in html              # token entry form present
    assert "setInterval(refresh,5000)" in html  # v1 auto-refresh kept
    assert "location.hash" in html or "history.replaceState" in html
    assert "Waiting approval" in html and "badge" in html
