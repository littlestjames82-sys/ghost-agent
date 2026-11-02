"""Ghost Agent v5 SANITY GATE — known answers, run FIRST.

v5 is a pure reskin (Trust Ladder design language). The gate pins
the new design landmarks AND that nothing behavioral drifted.

K1 Back-compat: v1+v2+v3+v4 gates (20 tests) pass in the same run
   (subprocess, as in v4 K6).
K2 Shelf semantics: the served page's Memory page carries a
   dashed-amber candidates shelf and a solid-green trusted shelf
   (distinguishing classes/styles asserted), and the Board renders
   its columns as shelves.
K3 Drawer completeness: the All Apps drawer lists each of the 11
   pages exactly once, grouped under OPERATE / INTELLIGENCE /
   SYSTEM labels, in that order.
K4 Bottom nav: the page has a bottom-nav element whose 4 targets
   map to overview, board, chat, and drawer-open; the <=820px
   media query shows the bottom nav and hides the sidebar.
K5 No behavior drift: on seeded data, /api/board, /api/overview,
   and /api/approvals return exactly what the (unchanged) payload
   functions return, and the token gate still 401s without a token.
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
from ghost_agent.models import Router
from ghost_agent.server import (approvals_payload, board_payload,
                                make_server, overview_payload)
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["overview", "board", "approvals", "tasks", "chat", "memory",
         "skills", "analytics", "logs", "schedules", "system"]


def _start(store, agent, token=None):
    server = make_server("127.0.0.1", 0, store=store, agent=agent, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
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


def _page(base):
    status, body = _req(base, "/")
    assert status == 200
    return body["_raw"]


def _mk(tmp_path):
    store = Store(tmp_path / "home")
    return store, Agent(store, router=Router(prefer_real=False))


# ---- K1 -----------------------------------------------------------------
def test_k1_prior_gates_still_pass():
    env = {"PYTHONPATH": "src", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    import os
    full_env = dict(os.environ, **env)
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py", "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "20 passed" in out.stdout


# ---- K2 -----------------------------------------------------------------
def test_k2_shelf_semantics(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        html = _page(base)
    finally:
        server.shutdown(); server.server_close()
    # Memory = the Trust Ladder: dashed amber candidates, solid green trusted.
    assert 'id="mem-candidates-shelf"' in html
    assert 'id="mem-trusted-shelf"' in html
    assert ".shelf-amber{border:1.5px dashed var(--amber)}" in html
    assert ".shelf-green{border:1.5px solid var(--greenline)}" in html
    # Both shelf color treatments appear for Memory (static) AND Skills (JS).
    assert html.count("shelf shelf-amber") >= 2
    assert html.count("shelf shelf-green") >= 2
    # Board columns render as shelves, one color per state: the JS
    # concatenates class='col shelf ' + colShelf[k] from this map.
    assert "col shelf \"+colShelf[k]+" in html
    assert "colShelf=" in html
    for cls in ("shelf-violet", "shelf-amber", "shelf-green", "shelf-red"):
        assert cls in html


# ---- K3 -----------------------------------------------------------------
def test_k3_drawer_completeness(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        html = _page(base)
    finally:
        server.shutdown(); server.server_close()
    assert 'id="drawer"' in html
    drawer = html.split('id="drawer"', 1)[1].split('id="palette"', 1)[0]
    for page in PAGES:
        assert drawer.count(f'data-page="{page}"') == 1, page
    for group in ("OPERATE", "INTELLIGENCE", "SYSTEM"):
        assert group in drawer
    assert drawer.index("OPERATE") < drawer.index("INTELLIGENCE") \
        < drawer.index("SYSTEM")
    # Grouping sanity: schedules lives under OPERATE, system under SYSTEM.
    assert drawer.index('data-page="schedules"') < drawer.index("INTELLIGENCE")
    assert drawer.index('data-page="system"') > drawer.index("SYSTEM")


# ---- K4 -----------------------------------------------------------------
def test_k4_bottom_nav(tmp_path):
    store, agent = _mk(tmp_path)
    server, base = _start(store, agent)
    try:
        html = _page(base)
    finally:
        server.shutdown(); server.server_close()
    assert 'id="bottomnav"' in html
    nav = html.split('id="bottomnav"', 1)[1].split("</nav>", 1)[0]
    for target in ("overview", "board", "chat", "drawer"):
        assert nav.count(f'data-nav="{target}"') == 1, target
    # Desktop default: bottom nav hidden.
    assert ".bottomnav{display:none" in html
    # <=820px: bottom nav shows, sidebar hides.
    mobile = html.split("@media (max-width:820px)", 1)[1].split("</style>", 1)[0]
    assert ".bottomnav{display:flex" in mobile
    assert ".sidebar{display:none" in mobile


# ---- K5 -----------------------------------------------------------------
@pytest.fixture()
def seeded(tmp_path):
    store, agent = _mk(tmp_path)
    done = agent.create_task("demo", title="Gate demo")
    agent.run_task(done["id"])
    agent.approve_task(done["id"])            # -> done, approval granted
    wait = agent.create_task("write gate.txt with: gate-content",
                             title="Gate write")
    agent.run_task(wait["id"])                # -> waiting_approval
    fail = agent.create_task("write nope.txt with: x", title="Gate deny")
    agent.run_task(fail["id"])
    agent.deny_task(fail["id"])               # -> failed, approval denied
    return store, agent, (done, wait, fail)


def test_k5_no_behavior_drift(seeded):
    store, agent, (done, wait, fail) = seeded
    server, base = _start(store, agent)
    try:
        status, board = _req(base, "/api/board")
        assert status == 200
        assert board == board_payload(agent)
        assert [t["id"] for t in board["columns"]["done"]] == [done["id"]]
        assert [t["id"] for t in board["columns"]["waiting_approval"]] == [wait["id"]]
        assert [t["id"] for t in board["columns"]["failed"]] == [fail["id"]]

        status, ov = _req(base, "/api/overview")
        assert status == 200
        assert ov == overview_payload(store, agent)
        assert ov["counts"] == {"total": 3, "running": 0,
                                 "waiting_approval": 1, "done": 1, "failed": 1}

        status, ap = _req(base, "/api/approvals")
        assert status == 200
        assert ap["approvals"] == approvals_payload(store, agent)
        assert [a["status"] for a in ap["approvals"]] == \
            ["denied", "pending", "approved"]
    finally:
        server.shutdown(); server.server_close()

    # Token gate still fails closed at the API.
    server, base = _start(store, agent, token="gate-v5-secret")
    try:
        status, _ = _req(base, "/api/board")
        assert status == 401
        status, board = _req(base, "/api/board",
                             headers={"X-Ghost-Token": "gate-v5-secret"})
        assert status == 200 and "columns" in board
        # ...and the page itself still loads so a human can enter the token.
        status, _ = _req(base, "/")
        assert status == 200
    finally:
        server.shutdown(); server.server_close()
