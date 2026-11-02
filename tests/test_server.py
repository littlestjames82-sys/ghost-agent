import json
import threading
import urllib.request

import pytest

from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.server import board_payload, make_server
from ghost_agent.store import Store


@pytest.fixture()
def running_server(tmp_path):
    store = Store(tmp_path / "home")
    agent = Agent(store, router=Router(prefer_real=False))
    server = make_server("127.0.0.1", 0, store=store, agent=agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, store, agent
    server.shutdown()
    server.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


def _post(base, path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


def test_board_payload_shape(store):
    agent = Agent(store, router=Router(prefer_real=False))
    payload = board_payload(agent)
    assert set(payload["columns"]) == {"running", "waiting_approval", "done", "failed"}
    assert payload["pending_approvals"] == []


def test_dashboard_page_and_warning(running_server):
    base, _, _ = running_server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        html = r.read().decode()
    assert "Ghost Agent" in html
    assert "NO AUTHENTICATION" in html


def test_api_flow_create_approve_board(running_server):
    base, store, _ = running_server
    status, body = _post(base, "/api/tasks", {"goal": "write api.txt with: via-api"})
    assert status == 201
    task = body["task"]
    assert task["status"] == "waiting_approval"

    _, board = _get(base, "/api/board")
    assert set(board["columns"]) == {"running", "waiting_approval", "done", "failed"}
    assert [t["id"] for t in board["columns"]["waiting_approval"]] == [task["id"]]
    assert len(board["pending_approvals"]) == 1

    status, body = _post(base, f"/api/tasks/{task['id']}/approve")
    assert status == 200 and body["task"]["status"] == "done"
    assert (store.workspace / "api.txt").read_text() == "via-api"

    _, board = _get(base, "/api/board")
    assert [t["id"] for t in board["columns"]["done"]] == [task["id"]]

    _, detail = _get(base, f"/api/tasks/{task['id']}")
    assert detail["task"]["id"] == task["id"] and detail["trail"]

    _, mem = _get(base, "/api/memory")
    assert "trusted" in mem and "candidates" in mem
    _, skills = _get(base, "/api/skills")
    assert len(skills["skills"]) == 1
    _, trail = _get(base, "/api/trail")
    assert any(e["type"] == "tool_execute" for e in trail["trail"])


def test_api_deny(running_server):
    base, store, _ = running_server
    _, body = _post(base, "/api/tasks", {"goal": "write deny.txt with: x"})
    tid = body["task"]["id"]
    status, body = _post(base, f"/api/tasks/{tid}/deny")
    assert status == 200 and body["task"]["status"] == "failed"
    assert not (store.workspace / "deny.txt").exists()
