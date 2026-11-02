"""Ghost Agent v4 feature tests: schedules CRUD + validation over
HTTP, scheduler daily math, search endpoint edge cases, export
endpoint, decide_all empty case, retry 404, and the v4 page
landmarks (Schedules page, Chat page, palette, global search,
density toggle, bulk-approval buttons)."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.scheduler import create_schedule, tick
from ghost_agent.server import make_server, search_payload
from ghost_agent.store import Store

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


# ---- schedules over HTTP --------------------------------------------------
def test_schedules_crud_and_validation(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/schedules", method="POST",
                            payload={"goal": "read a.txt", "kind": "interval",
                                     "interval_minutes": 15})
        assert status == 201
        sched = body["schedule"]
        assert sched["interval_minutes"] == 15
        assert sched["enabled"] is True
        assert sched["next_run_at"]
        sid = sched["id"]
        # validation failures -> 400, nothing extra stored
        for bad in ({"goal": "", "kind": "interval", "interval_minutes": 5},
                    {"goal": "  ", "kind": "interval", "interval_minutes": 5},
                    {"goal": "read a.txt", "kind": "interval",
                     "interval_minutes": 0},
                    {"goal": "read a.txt", "kind": "interval"},
                    {"goal": "read a.txt", "kind": "daily",
                     "time_hhmm": "25:00"},
                    {"goal": "read a.txt", "kind": "daily",
                     "time_hhmm": "9am"},
                    {"goal": "read a.txt", "kind": "weekly"}):
            status, body = _req(base, "/api/schedules", method="POST",
                                payload=bad)
            assert status == 400, bad
            assert "error" in body
        status, body = _req(base, "/api/schedules")
        assert [s["id"] for s in body["schedules"]] == [sid]
        # daily create works
        status, body = _req(base, "/api/schedules", method="POST",
                            payload={"goal": "read b.txt", "kind": "daily",
                                     "time_hhmm": "07:30", "title": "Morning"})
        assert status == 201
        daily = body["schedule"]
        assert daily["time_hhmm"] == "07:30"
        assert daily["title"] == "Morning"
        # toggle flips enabled both ways
        status, body = _req(base, f"/api/schedules/{sid}/toggle", method="POST")
        assert status == 200 and body["schedule"]["enabled"] is False
        status, body = _req(base, f"/api/schedules/{sid}/toggle", method="POST")
        assert status == 200 and body["schedule"]["enabled"] is True
        # run now: creates + runs a task immediately, updates last_*
        status, body = _req(base, f"/api/schedules/{sid}/run", method="POST")
        assert status == 201
        assert body["task"]["id"]
        assert body["schedule"]["last_task_id"] == body["task"]["id"]
        assert body["schedule"]["last_run_at"]
        assert store.get("tasks", body["task"]["id"]) is not None
        # unknown ids -> 404
        for method, path in (("POST", "/api/schedules/nope/toggle"),
                             ("POST", "/api/schedules/nope/run"),
                             ("DELETE", "/api/schedules/nope")):
            status, _ = _req(base, path, method=method)
            assert status == 404, (method, path)
        # delete works and is gone from the list
        status, body = _req(base, f"/api/schedules/{sid}", method="DELETE")
        assert status == 200 and body["deleted"] == sid
        status, body = _req(base, "/api/schedules")
        assert [s["id"] for s in body["schedules"]] == [daily["id"]]
    finally:
        server.shutdown(); server.server_close()


# ---- scheduler daily math (unit) -------------------------------------------
def test_scheduler_daily_next_run_math(tmp_path):
    store, agent = _mk(tmp_path)
    morning = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    s = create_schedule(store, "read d.txt", kind="daily",
                        time_hhmm="10:30", now=morning)
    assert s["next_run_at"] == datetime(2026, 3, 2, 10, 30, tzinfo=UTC).isoformat()
    late = datetime(2026, 3, 2, 11, 0, tzinfo=UTC)
    s2 = create_schedule(store, "read e.txt", kind="daily",
                         time_hhmm="10:30", now=late)
    assert s2["next_run_at"] == datetime(2026, 3, 3, 10, 30, tzinfo=UTC).isoformat()
    # the first schedule fires at its time and advances a full day
    fired = tick(store, agent, now=datetime(2026, 3, 2, 10, 30, tzinfo=UTC))
    assert fired == [s["id"]]
    after = store.get("schedules", s["id"])
    assert after["next_run_at"] == datetime(2026, 3, 3, 10, 30, tzinfo=UTC).isoformat()
    assert after["last_run_at"] == datetime(2026, 3, 2, 10, 30, tzinfo=UTC).isoformat()
    # and not again the same day
    assert tick(store, agent, now=datetime(2026, 3, 2, 18, 0, tzinfo=UTC)) == []


def test_schedule_create_unit_validation(tmp_path):
    store, _ = _mk(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        create_schedule(store, "", kind="interval", interval_minutes=5)
    with pytest.raises(ValueError):
        create_schedule(store, "read x", kind="interval", interval_minutes=0)
    with pytest.raises(ValueError):
        create_schedule(store, "read x", kind="daily", time_hhmm="24:00")
    with pytest.raises(ValueError):
        create_schedule(store, "read x", kind="hourly")
    assert store.list("schedules") == []


# ---- search endpoint edge cases ---------------------------------------------
def test_search_payload_cap_and_empty(store, agent):
    for i in range(10):
        agent.create_task("read x.txt", title=f"capcap task {i}")
    out = search_payload(store, "capcap")
    assert len(out["tasks"]) == 8          # group cap
    assert search_payload(store, "") == {
        "tasks": [], "memories": [], "skills": [], "trail": []}
    assert search_payload(store, "   ") == {
        "tasks": [], "memories": [], "skills": [], "trail": []}
    out = search_payload(store, "CAPCAP")  # case-insensitive
    assert len(out["tasks"]) == 8


def test_search_endpoint_over_http(tmp_path):
    store, agent = _mk(tmp_path)
    agent.create_task("demo", title="Findable demo")
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/search?q=findable")
        assert status == 200
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["title"] == "Findable demo"
        status, body = _req(base, "/api/search?q=")
        assert status == 200
        assert body == {"tasks": [], "memories": [], "skills": [], "trail": []}
    finally:
        server.shutdown(); server.server_close()


# ---- export + decide_all/retry edges ----------------------------------------
def test_export_endpoint_shape(tmp_path):
    store, agent = _mk(tmp_path)
    task = agent.create_task("demo")
    agent.run_task(task["id"])
    agent.approve_task(task["id"])
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/export")
        assert status == 200
        assert body["version"] == "0.7.0"
        assert len(body["tasks"]) == 1
        assert len(body["approvals"]) == 1
        assert len(body["skills"]) == 1
        assert body["schedules"] == []
        assert len(body["trail"]) >= 11
    finally:
        server.shutdown(); server.server_close()


def test_export_content_disposition(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        req = urllib.request.Request(base + "/api/export", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            cd = r.headers.get("Content-Disposition") or ""
            assert "attachment" in cd
            assert "ghost-agent-export.json" in cd
    finally:
        server.shutdown(); server.server_close()


def test_decide_all_with_nothing_pending(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/approvals/decide_all", method="POST",
                            payload={"decision": "denied"})
        assert status == 200
        assert body["decided"] == 0 and body["results"] == []
        status, _ = _req(base, "/api/approvals/decide_all", method="POST",
                         payload={"decision": ""})
        assert status == 400
        status, _ = _req(base, "/api/approvals/decide_all", method="POST",
                         payload={})
        assert status == 400
    finally:
        server.shutdown(); server.server_close()


def test_retry_unknown_task_404(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        status, _ = _req(base, "/api/tasks/tsk_missing/retry", method="POST")
        assert status == 404
    finally:
        server.shutdown(); server.server_close()


# ---- page landmarks ----------------------------------------------------------
def test_v4_page_landmarks(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/")
        assert status == 200
        page = body["_raw"]
        for needle in (
            'data-page="schedules"', 'data-page="chat"',
            'id="page-schedules"', 'id="page-chat"',
            'id="sch-form"', 'id="sch-goal"', 'id="sch-kind"',
            'id="schedules"', 'id="sch-status"',
            'id="chat-form"', 'id="chat-input"', 'id="chat-thread"',
            'id="chat-picker"',
            'id="globalsearch"', 'id="search-results"',
            'id="palette"', 'id="palette-q"', 'id="palette-list"',
            'id="density-btn"', "toggleDensity", "openPalette",
            'id="ap-approve-all"', 'id="ap-deny-all"', "decideAll",
            'id="nav-schedules"', "sys-export", "retryTask",
            "localStorage", "/api/export",
        ):
            assert needle in page, needle
    finally:
        server.shutdown(); server.server_close()
