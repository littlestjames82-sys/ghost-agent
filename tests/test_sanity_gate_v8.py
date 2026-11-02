"""Ghost Agent v8 SANITY GATE — known answers, run FIRST.

v0.8.0 integrates the studio's repo ecosystem: a registry file
(GHOST_AGENT_HOME/ecosystem.json, seeded on first run from the
bundled 72-project seed), three READ-class tools (list_projects,
repo_status, read_project_file) that treat the registry as the
allowlist, and a 12th dashboard page (Ecosystem) backed by
GET /api/ecosystem. Writes stay confined to the agent's own
workspace; there are no network calls in this version.

K1 Seed: a fresh home seeds ecosystem.json from the bundled
   resource — 72 entries, ghost-kernel in layer 'kernel',
   ghost-agent in 'products'; the bundled resource parses.
K2 list_projects: all fixture entries returned; the layer filter
   returns exactly the matching layer; has_local is computed live
   (real dir -> true, missing path -> false). The loader prefers
   the home file over the bundle.
K3 repo_status: a fixture git repo reports its real branch, a
   dirty count of 1, and the real last-commit subject; a plain
   directory reports 'not a git repository'; a missing path
   reports 'no local tree' with the repo slug; an unknown project
   errors and names the registry as the fix.
K4 read_project_file: exact content from inside the root;
   '../' traversal refused; absolute-path escape refused;
   unregistered project refused; a >200KB file comes back
   truncated with the marker present.
K5 Governance: all three tools classify read (auto-allow), and
   MockModel tasks using them ('status of <project>',
   'list projects') run to done with NO approval ever created.
K6 HTTP + page: GET /api/ecosystem is 200 with the fixture
   entries and matching has_local flags; the served page has the
   Ecosystem section, sidebar nav entry, and a drawer listing
   each of the 12 pages exactly once; the page script passes
   node --check. Plus: POST /api/import IGNORES a snapshot's
   ecosystem key — the local registry never changes.
K7 Robustness: a home ecosystem.json with invalid JSON never
   crashes startup — the registry loads empty, the error is
   reported (payload load_error), and the user's file is NOT
   overwritten; entries missing required fields are skipped
   while valid entries load; the server still initializes and
   serves /api/ecosystem with the broken registry.
K8 Back-compat: gates v1..v7b (43 tests) pass in the same run
   (subprocess, as in prior gates).

Feature imports live inside the tests so the baseline run on
v0.7.0 code fails each case for its designed reason (the
ecosystem module / tools / route do not exist yet) instead of
dying at collection.
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
from ghost_agent.governance import classify
from ghost_agent.models import Router
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
PAGES12 = ["overview", "board", "approvals", "tasks", "chat", "memory",
           "skills", "analytics", "logs", "schedules", "system",
           "ecosystem"]


# ---- fixtures ------------------------------------------------------------

def _git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(path),
         "-c", "user.email=gate@example.invalid", "-c", "user.name=Gate",
         *args],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _fixture_env(tmp_path: Path):
    """A store home whose ecosystem.json is a 3-project fixture:
    alpha-proj (kernel, a real git repo, one commit, then one
    dirtied file), beta-proj (law, a plain directory),
    gamma-proj (tools, a path that does not exist)."""
    repo = tmp_path / "alpha"
    repo.mkdir()
    (repo / "tracked.txt").write_text("tracked contents\n", encoding="utf-8")
    (repo / "notes.txt").write_text("alpha notes exact\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "alpha initial commit")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    with open(repo / "tracked.txt", "a", encoding="utf-8") as fh:
        fh.write("dirty line\n")  # exactly one uncommitted change

    plain = tmp_path / "beta"
    plain.mkdir()
    (plain / "readme.txt").write_text("beta\n", encoding="utf-8")

    missing = tmp_path / "gamma-absent"

    store = Store(tmp_path / "home")
    fixture = [
        {"name": "alpha-proj", "repo": "littlestjames82-sys/alpha-proj",
         "local_path": str(repo), "layer": "kernel",
         "role": "fixture kernel project", "source": "gate fixture"},
        {"name": "beta-proj", "repo": None,
         "local_path": str(plain), "layer": "law",
         "role": "fixture plain directory project",
         "source": "gate fixture"},
        {"name": "gamma-proj", "repo": "littlestjames82-sys/gamma-proj",
         "local_path": str(missing), "layer": "tools",
         "role": "fixture missing project", "source": "gate fixture"},
    ]
    (store.home / "ecosystem.json").write_text(
        json.dumps(fixture), encoding="utf-8")
    return store, repo, branch


def _tools(store):
    from ghost_agent.ecosystem import get_ecosystem
    from ghost_agent.tools import Tools
    return Tools(store.workspace, ecosystem=get_ecosystem(store))


def _refused(fn) -> bool:
    """A read_project_file refusal may be a ToolError raise (the
    sandbox style) or an ok:False result — both are refusals."""
    from ghost_agent.tools import ToolError
    try:
        res = fn()
    except ToolError:
        return True
    return isinstance(res, dict) and not res.get("ok")


def _start(store, agent, token=None):
    from ghost_agent.server import make_server
    server = make_server("127.0.0.1", 0, store=store, agent=agent,
                         token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
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


# ---- K1 -----------------------------------------------------------------
def test_k1_seed(tmp_path):
    from ghost_agent.ecosystem import Ecosystem, SEED_PATH

    bundled = json.loads(Path(SEED_PATH).read_text(encoding="utf-8"))
    assert isinstance(bundled, list) and len(bundled) == 72

    store = Store(tmp_path / "home")
    assert not (store.home / "ecosystem.json").exists()
    eco = Ecosystem(store)
    seeded_file = store.home / "ecosystem.json"
    assert seeded_file.exists()
    on_disk = json.loads(seeded_file.read_text(encoding="utf-8"))
    assert len(on_disk) == 72
    assert len(eco.entries) == 72
    assert eco.load_error is None and eco.skipped == 0
    by_name = {e["name"]: e for e in eco.entries}
    assert by_name["ghost-kernel"]["layer"] == "kernel"
    assert by_name["ghost-agent"]["layer"] == "products"


# ---- K2 -----------------------------------------------------------------
def test_k2_list_projects(tmp_path):
    store, repo, _branch = _fixture_env(tmp_path)
    tools = _tools(store)

    res = tools.list_projects()
    assert res["ok"]
    projects = res["projects"]
    assert [p["name"] for p in projects] == \
        ["alpha-proj", "beta-proj", "gamma-proj"]
    by_name = {p["name"]: p for p in projects}
    assert by_name["alpha-proj"]["has_local"] is True
    assert by_name["beta-proj"]["has_local"] is True
    assert by_name["gamma-proj"]["has_local"] is False
    assert by_name["alpha-proj"]["layer"] == "kernel"
    assert "alpha-proj" in res["output"]

    only = tools.list_projects(layer="kernel")
    assert [p["name"] for p in only["projects"]] == ["alpha-proj"]
    none = tools.list_projects(layer="products")
    assert none["projects"] == []


# ---- K3 -----------------------------------------------------------------
def test_k3_repo_status(tmp_path):
    store, repo, branch = _fixture_env(tmp_path)
    tools = _tools(store)

    res = tools.repo_status("alpha-proj")
    assert res["ok"] and res["has_local"] is True
    assert res["is_git_repo"] is True
    assert res["branch"] == branch
    assert res["dirty_count"] == 1
    assert res["last_commit"]["subject"] == "alpha initial commit"
    assert res["last_commit"]["hash"]
    assert "alpha-proj" in res["output"]

    res = tools.repo_status("beta-proj")
    assert res["ok"] and res["has_local"] is True
    assert res["is_git_repo"] is False
    assert "not a git repository" in res["output"]

    res = tools.repo_status("gamma-proj")
    assert res["ok"] and res["has_local"] is False
    assert "no local tree" in res["output"]
    assert "littlestjames82-sys/gamma-proj" in res["output"]

    res = tools.repo_status("nope-proj")
    assert not res["ok"]
    assert "registry" in (res.get("error") or "")


# ---- K4 -----------------------------------------------------------------
def test_k4_read_project_file(tmp_path):
    store, repo, _branch = _fixture_env(tmp_path)
    tools = _tools(store)
    outside = tmp_path / "outside.txt"
    outside.write_text("must never be read\n", encoding="utf-8")

    res = tools.read_project_file("alpha-proj", "notes.txt")
    assert res["ok"] and res["output"] == "alpha notes exact\n"

    res = tools.read_project_file("alpha-proj", "missing.txt")
    assert not res["ok"]

    assert _refused(lambda: tools.read_project_file(
        "alpha-proj", "../outside.txt"))
    assert _refused(lambda: tools.read_project_file(
        "alpha-proj", str(outside)))

    res = tools.read_project_file("nope-proj", "notes.txt")
    assert not res["ok"] and "registry" in (res.get("error") or "")

    big = repo / "big.txt"
    big.write_text("a" * 250_000, encoding="utf-8")
    res = tools.read_project_file("alpha-proj", "big.txt")
    assert res["ok"] and res["truncated"] is True
    assert "truncated" in res["output"]
    assert len(res["output"]) <= 200_000 + 200


# ---- K5 -----------------------------------------------------------------
def test_k5_governance_and_mock_patterns(tmp_path):
    for tool in ("list_projects", "repo_status", "read_project_file"):
        decision = classify(tool)
        assert decision.action == "allow", tool
        assert decision.kind == "read", tool

    store, _repo, _branch = _fixture_env(tmp_path)
    agent = Agent(store, router=Router(prefer_real=False))

    task = agent.create_task("status of alpha-proj")
    assert task["steps"][0]["tool"] == "repo_status"
    assert task["steps"][0]["args"] == {"project": "alpha-proj"}
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert store.list("approvals") == []

    task = agent.create_task("list projects")
    assert task["steps"][0]["tool"] == "list_projects"
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert store.list("approvals") == []

    # An unregistered name falls through to the old behavior
    # (echo_note), never a repo_status step at empty air.
    task = agent.create_task("status of nope-proj")
    assert task["steps"][0]["tool"] != "repo_status"


# ---- K6 -----------------------------------------------------------------
def test_k6_http_and_page(tmp_path):
    store, _repo, _branch = _fixture_env(tmp_path)
    agent = Agent(store, router=Router(prefer_real=False))
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/ecosystem")
        assert status == 200
        assert body["count"] == 3
        by_name = {p["name"]: p for p in body["projects"]}
        assert by_name["alpha-proj"]["has_local"] is True
        assert by_name["beta-proj"]["has_local"] is True
        assert by_name["gamma-proj"]["has_local"] is False
        assert body["load_error"] is None

        status, body = _req(base, "/")
        assert status == 200
        html = body["_raw"]
        assert 'id="page-ecosystem"' in html
        assert 'data-page="ecosystem"' in html
        drawer = html.split('id="drawer"', 1)[1].split('id="palette"', 1)[0]
        for page in PAGES12:
            assert drawer.count(f'data-page="{page}"') == 1, page
        assert drawer.index("INTELLIGENCE") < drawer.index(
            'data-page="ecosystem"') < drawer.index("SYSTEM")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        js = tmp_path / "page.js"
        js.write_text(script, encoding="utf-8")
        out = subprocess.run(["node", "--check", str(js)],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr

        # Import must IGNORE the snapshot's ecosystem key: the
        # registry is machine-local config, never portable data.
        from ghost_agent.server import export_payload
        before = (store.home / "ecosystem.json").read_bytes()
        snapshot = export_payload(store)
        snapshot["ecosystem"] = [{"name": "evil-proj", "layer": "kernel",
                                   "role": "must not land",
                                   "repo": None, "local_path": None,
                                   "source": "hostile snapshot"}]
        status, body = _req(base, "/api/import", method="POST",
                            body=snapshot)
        assert status == 200 and "imported" in body
        status, body = _req(base, "/api/ecosystem")
        assert status == 200 and body["count"] == 3
        assert "evil-proj" not in {p["name"] for p in body["projects"]}
        assert (store.home / "ecosystem.json").read_bytes() == before
    finally:
        server.shutdown(); server.server_close()


# ---- K7 -----------------------------------------------------------------
def test_k7_registry_robustness(tmp_path):
    from ghost_agent.ecosystem import Ecosystem

    store = Store(tmp_path / "home")
    broken = store.home / "ecosystem.json"
    broken.write_text("{not json at all", encoding="utf-8")
    eco = Ecosystem(store)  # must not raise
    assert eco.entries == []
    assert eco.load_error  # the failure is visible, in plain words
    # The user's (broken) file is theirs: never overwritten by us.
    assert broken.read_text(encoding="utf-8") == "{not json at all"

    store2 = Store(tmp_path / "home2")
    (store2.home / "ecosystem.json").write_text(json.dumps([
        {"name": "ok-one", "layer": "tools", "role": "fine",
         "repo": None, "local_path": None, "source": "gate"},
        {"repo": "littlestjames82-sys/nameless"},       # no name/layer
        "not-an-object",                                # not a dict
        {"name": "ok-two", "layer": "law"},
    ]), encoding="utf-8")
    eco2 = Ecosystem(store2)
    assert [e["name"] for e in eco2.entries] == ["ok-one", "ok-two"]
    assert eco2.skipped == 2

    # The server still initializes and serves with the broken one.
    agent = Agent(store, router=Router(prefer_real=False))
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "/api/ecosystem")
        assert status == 200
        assert body["count"] == 0
        assert body["load_error"]
    finally:
        server.shutdown(); server.server_close()


# ---- K8 -----------------------------------------------------------------
def test_k8_prior_gates_still_pass():
    import os
    full_env = dict(os.environ, PYTHONPATH="src",
                    PATH="/usr/bin:/bin:/usr/local/bin")
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py",
         "tests/test_sanity_gate_v5.py", "tests/test_sanity_gate_v6.py",
         "tests/test_sanity_gate_v7.py", "tests/test_sanity_gate_v7b.py",
         "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=900)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "43 passed" in out.stdout
