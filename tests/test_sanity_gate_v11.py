"""Ghost Agent v11 SANITY GATE — known answers, run FIRST.

v0.11.0 is "Ghost Intelligence": the intelligence pass on top of
the v0.10.0 profiles/router.

1. MEMORY-INFORMED PLANNING. At task creation the agent selects
   TRUSTED memories (candidates NEVER) whose text shares keyword
   overlap with the goal (tokenized on non-alphanumerics,
   lowercased, stopwords and <3-char tokens dropped; score =
   distinct shared tokens; keep score>=1, top 5, score desc then
   store order). The chosen ids ride on the task record
   (memory_context), their texts are rendered into EVERY planner
   prompt as a "Relevant memory (trusted, user-approved):" block,
   and exactly ONE memory_recalled trail event is emitted (only
   when the list is non-empty — a fresh-home CLI demo still
   produces exactly 19 trail events).
2. SKILL HINTS. The same pass selects TRUSTED skills only (top 2)
   whose name / source goal overlaps the goal. The prompt gains
   "Relevant skill '<name>' (trusted): a previous successful run
   used these tools in order: t1 -> t2 -> ..."; the ids ride on
   the task (skill_hints) with ONE skill_hinted trail event.
3. VERIFIER (opt-in). create_task(verify=True) / POST /api/tasks
   {"verify": true} / demo --verify. When such a task reaches
   done: a MockModel-owned task records verification
   {"outcome": "skipped", "reason": "mock model"} with NO model
   call; otherwise ONE extra call through the SAME provider that
   ran the task judges the deliverable (the files its write steps
   produced, read back from the workspace; else the step outputs)
   against the goal, demanding a strict JSON verdict. Outcomes:
   grounded / issues (list recorded verbatim) / unparseable /
   error — the verdict NEVER changes the task's status, its usage
   is recorded like any other call, and "over_budget": true is
   noted when the verifier's usage trips the token budget.
4. TIERS. Profiles gain an optional tier (frontier|standard|
   small). Effective tier = the file's valid tier, else the
   preset tier for a same-named profile, else None — a
   v0.10.0-shaped providers.json (no tiers) still yields preset
   tiers, and the user's file is NEVER rewritten. Invalid tier
   values are ignored for selection but shown raw in /api/system
   (tier_configured). New strategy "best": highest effective tier
   among CONFIGURED profiles wins (frontier > standard > small >
   untiered), ties by stored order, none configured -> "default"
   behavior. Unknown strategy strings behave as "default".
   /api/system shows the effective tier per profile.
5. TRAINING EXPORT. GET /api/export-training?include_failed=1
   and `ghost-agent export-training [--out PATH]
   [--include-failed]` emit JSONL: one line per DONE task
   (failed only with the flag, status marked) carrying task_id,
   goal, title, provider, model, status, created_at, executed
   steps (tool/args/status/output <=2000 chars), approvals
   ({tool, decision}) derived from the trail, and usage_totals —
   plus a data card (counts by status and provider, total
   tokens). No providers config, no memory/skill records, no key
   values.
6. NAMING. The dashboard System page's providers block is headed
   "Ghost Intelligence — models & routing"; the Chat composer
   gains a "Verify result" checkbox sending verify: true. The
   drawer stays exactly 12 pages.

I1 trusted memory in every planner prompt; a stronger-overlap
   CANDIDATE memory never appears and is not in memory_context.
I2 no match -> no block, no event; CLI demo trail stays 19.
I3 trusted skill injects its tool sequence; candidate never does.
I4 verifier: grounded / issues verbatim / unparseable / skipped
   for mock (zero calls) / error keeps task done / off -> no
   verification field or events; HTTP verify flag.
I5 best strategy ranking, untiered last, ties by stored order,
   none configured -> default; tier merge from a v0.10.0 file.
I6 export shape, failed exclusion/inclusion, no config or key
   leakage, data-card counts; the CLI writes the same JSONL.
I7 /api/system effective tiers; the page carries "Ghost
   Intelligence" + the verify checkbox and exactly 12 drawer
   pages.
I8 the verifier prompt carries the SAME planner context (the
   recalled trusted memory texts + skill hints, under a "Context
   the planner was given" heading): a candidate memory reaches
   NEITHER prompt, and a task with no recalled context produces
   a verifier prompt with no context section.
P11 Back-compat: gates v1..v10 (77 tests) pass in a subprocess.

Feature assertions live inside the tests (and feature imports
are lazy) so the baseline run on v0.10.0 code fails each case
for its designed reason instead of dying at collection.
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
from ghost_agent.skills import Skills
from ghost_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent

# The tests' own HTTP calls must bypass the urlopen stub (the
# monkeypatch target is the stdlib attribute itself), so keep a
# pristine reference captured at import time.
REAL_URLOPEN = urllib.request.urlopen

SMALL = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


# ---- stub provider plumbing: dispatch on the request URL -------------------
class _StubResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _step_payload(step, usage, model="stub-model"):
    return {"model": model,
            "choices": [{"message": {"content": json.dumps(step)}}],
            "usage": usage}


def _verdict_payload(content, usage=SMALL, model="stub-model"):
    return {"model": model,
            "choices": [{"message": {"content": content}}],
            "usage": usage}


def _echo(text="probe"):
    return {"tool": "echo_note", "args": {"text": text},
            "verify": {"type": "ok"}}


class _Script:
    """One route's scripted outcomes: payloads to return or
    exceptions to raise, in call order (the last one repeats)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, record):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1,
                                     len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _DispatchStub:
    """urlopen replacement: routes each request to the handler
    whose match string appears in the URL, and records every
    request (url, lowercased headers, parsed JSON body)."""

    def __init__(self):
        self.routes = []      # [(match, handler)]
        self.records = []

    def add(self, match, handler):
        self.routes.append((match, handler))
        return handler

    def calls_to(self, match):
        return [r for r in self.records if match in r["url"]]

    def prompts(self):
        """Every planner/verifier prompt sent so far, in order."""
        out = []
        for r in self.records:
            msgs = (r["body"] or {}).get("messages") or []
            if msgs and isinstance(msgs[0].get("content"), str):
                out.append(msgs[0]["content"])
        return out

    def __call__(self, req, timeout=None):
        body = {}
        if req.data:
            try:
                body = json.loads(req.data.decode())
            except ValueError:
                body = {}
        record = {"url": req.full_url,
                  "headers": {k.lower(): v for k, v in req.header_items()},
                  "body": body}
        self.records.append(record)
        for match, handler in self.routes:
            if match in req.full_url:
                return _StubResponse(handler(record))
        raise AssertionError(f"no stub route for {req.full_url}")


def _write_providers(store, doc):
    (store.home / "providers.json").write_text(
        json.dumps(doc), encoding="utf-8")


def _profile(base_url, model, key_env, kind="openai-compatible", tier=None):
    prof = {"base_url": base_url, "model": model,
            "api_key_env": key_env, "kind": kind}
    if tier is not None:
        prof["tier"] = tier
    return prof


def _one_profile_doc(**kw):
    return {"strategy": kw.get("strategy", "default"),
            "default": kw.get("default", "prof"),
            "chain": kw.get("chain", []),
            "profiles": {
                "prof": _profile("http://prof.invalid/v1", "stub-model",
                                 "GA_V11_KEY")}}


def _agent(tmp_path, stub, monkeypatch, doc=None, name="home"):
    """An Agent over a fresh store whose home holds the fixture
    providers.json (when given), with urlopen stubbed and a
    mock-only-by-default Router (providers do the routing)."""
    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen", stub)
    store = Store(tmp_path / name)
    if doc is not None:
        _write_providers(store, doc)
    return store, Agent(store, router=Router(prefer_real=False))


def _mock_agent(tmp_path, name="home"):
    store = Store(tmp_path / name)
    return store, Agent(store, router=Router(prefer_real=False))


def _ok_script(model="stub-model"):
    return _Script(_step_payload(_echo(), SMALL, model=model),
                   _step_payload({"tool": "done"}, SMALL, model=model))


def _trail(store, task_id, type_):
    return [e for e in store.read_trail(task_id=task_id, limit=100000)
            if e["type"] == type_]


def _start(store, agent):
    from ghost_agent.server import make_server
    server = make_server("127.0.0.1", 0, store=store, agent=agent,
                         token=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with REAL_URLOPEN(req, timeout=10) as r:
            payload = r.read()
            return r.status, (payload if raw else
                              json.loads(payload.decode() or "{}"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, (payload if raw else
                        json.loads(payload.decode() or "{}"))


# ---- I1 ---------------------------------------------------------------------
def test_i1_trusted_memory_recalled_candidate_never(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_one_profile_doc())
    mem = Memory(store)
    trusted = mem.propose(
        "Zephyr billing exports are archived monthly in the ledger",
        source="gate")
    mem.approve(trusted["id"])
    # A CANDIDATE with STRONGER goal overlap: it must never leak.
    mem.propose("zephyr billing vault summarize quarterly records "
                "folder index", source="gate")
    task = agent.create_task("Summarize zephyr billing in the vault")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    prompts = stub.prompts()
    assert len(prompts) >= 2                      # create + loop call
    for p in prompts:                             # EVERY planner prompt
        assert "Relevant memory (trusted, user-approved):" in p
        assert "archived monthly in the ledger" in p
        assert "quarterly records folder index" not in p
    assert task["memory_context"] == [trusted["id"]]
    events = _trail(store, task["id"], "memory_recalled")
    assert len(events) == 1
    assert events[0]["detail"]["memory_ids"] == [trusted["id"]]


# ---- I2 ---------------------------------------------------------------------
def test_i2_no_match_no_block_no_event(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_one_profile_doc())
    mem = Memory(store)
    other = mem.propose("Cooking recipes for winter soups and stews",
                        source="gate")
    mem.approve(other["id"])                      # trusted, zero overlap
    task = agent.create_task("Summarize zephyr billing records")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["memory_context"] == []
    for p in stub.prompts():
        assert "Relevant memory" not in p
    assert _trail(store, task["id"], "memory_recalled") == []


def test_i2_cli_demo_trail_stays_19(tmp_path):
    home = tmp_path / "demo-home"
    env = dict(os.environ, GHOST_AGENT_HOME=str(home),
               PYTHONPATH="src", PATH="/usr/bin:/bin:/usr/local/bin")
    env.pop("GHOST_AGENT_API_KEY", None)
    out = subprocess.run(
        [sys.executable, "-m", "ghost_agent", "demo"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = (home / "trail.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 19


# ---- I3 ---------------------------------------------------------------------
def test_i3_trusted_skill_hint_candidate_never(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_one_profile_doc())
    skills = Skills(store)
    trusted = skills.propose_from_run({
        "id": "tsk_fixture_trusted", "title": "Zephyr Billing Export",
        "goal": "Export the zephyr billing ledger",
        "steps": [{"tool": "read_file", "args": {"path": "ledger.csv"}},
                  {"tool": "write_file",
                   "args": {"path": "out.md", "content": "x"}}]})
    skills.promote(trusted["id"])
    # A CANDIDATE whose name overlaps the goal even more strongly.
    candidate = skills.propose_from_run({
        "id": "tsk_fixture_candidate",
        "title": "Zephyr Billing Export Run Again",
        "goal": "Run the zephyr billing export again",
        "steps": [{"tool": "echo_note", "args": {"text": "hi"}}]})
    task = agent.create_task("Run the zephyr billing export again")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    prompts = stub.prompts()
    assert prompts
    for p in prompts:
        assert "Relevant skill 'zephyr-billing-export' (trusted):" in p
        assert "read_file -> write_file" in p
        assert candidate["name"] not in p
    assert task["skill_hints"] == [trusted["id"]]
    events = _trail(store, task["id"], "skill_hinted")
    assert len(events) == 1
    assert events[0]["detail"]["skill_ids"] == [trusted["id"]]


# ---- I4 ---------------------------------------------------------------------
def _verifier_agent(tmp_path, monkeypatch, script):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", script)
    return _agent(tmp_path, stub, monkeypatch, doc=_one_profile_doc())


def test_i4_verifier_grounded(tmp_path, monkeypatch):
    script = _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        _verdict_payload(json.dumps({"grounded": True, "issues": []})))
    store, agent = _verifier_agent(tmp_path, monkeypatch, script)
    task = agent.create_task("write the thing", verify=True)
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    verdict = task["verification"]
    assert verdict["outcome"] == "grounded"
    assert verdict["issues"] == []
    assert verdict["provider"] == "prof"
    events = _trail(store, task["id"], "verification")
    assert len(events) == 1
    assert events[0]["detail"]["outcome"] == "grounded"
    # The verifier's usage is recorded like any other call:
    # create + loop + verifier = 3 calls.
    assert task["usage_totals"]["calls"] == 3
    assert len(_trail(store, task["id"], "model_usage")) == 3


def test_i4_verifier_issues_verbatim(tmp_path, monkeypatch):
    issues = ["missing totals", "wrong date"]
    script = _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        _verdict_payload(json.dumps({"grounded": False,
                                     "issues": issues})))
    store, agent = _verifier_agent(tmp_path, monkeypatch, script)
    task = agent.create_task("write the thing", verify=True)
    task = agent.run_task(task["id"])
    assert task["status"] == "done"               # verdict is evidence
    assert task["verification"]["outcome"] == "issues"
    assert task["verification"]["issues"] == issues


def test_i4_verifier_unparseable(tmp_path, monkeypatch):
    script = _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        _verdict_payload("Looks fine to me, no JSON here."))
    store, agent = _verifier_agent(tmp_path, monkeypatch, script)
    task = agent.create_task("write the thing", verify=True)
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["verification"]["outcome"] == "unparseable"
    assert task["verification"]["issues"] == []


def test_i4_verifier_skipped_for_mock(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_stub(req, timeout=None):
        calls["n"] += 1
        raise AssertionError("no network in a MockModel run")

    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen",
                        counting_stub)
    store, agent = _mock_agent(tmp_path)
    task = agent.create_task("list projects in the ecosystem",
                             verify=True)
    task = agent.run_task(task["id"])
    assert task["provider"] == "mock"
    assert task["status"] == "done"
    assert task["verification"] == {"outcome": "skipped",
                                    "reason": "mock model"}
    events = _trail(store, task["id"], "verification")
    assert len(events) == 1
    assert events[0]["detail"]["outcome"] == "skipped"
    assert calls["n"] == 0                        # zero extra calls


def test_i4_verifier_error_keeps_task_done(tmp_path, monkeypatch):
    script = _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        urllib.error.URLError("verifier boom"))
    store, agent = _verifier_agent(tmp_path, monkeypatch, script)
    task = agent.create_task("write the thing", verify=True)
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["verification"]["outcome"] == "error"
    assert task["verification"]["issues"] == []
    events = _trail(store, task["id"], "verification")
    assert len(events) == 1
    assert "verifier boom" in json.dumps(events[0]["detail"]) or \
        "URLError" in json.dumps(events[0]["detail"])


def test_i4_verify_off_no_field_no_events(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_one_profile_doc())
    task = agent.create_task("write the thing")   # verify not asked
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert "verification" not in task
    assert _trail(store, task["id"], "verification") == []


def test_i4_http_verify_flag(tmp_path, monkeypatch):
    script = _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        _verdict_payload(json.dumps({"grounded": True, "issues": []})))
    store, agent = _verifier_agent(tmp_path, monkeypatch, script)
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "POST", "/api/tasks",
                            {"goal": "write the thing", "verify": True})
        assert status == 201
        assert body["task"]["verification"]["outcome"] == "grounded"
        status, body = _req(base, "POST", "/api/tasks",
                            {"goal": "write the thing"})
        assert status == 201
        assert "verification" not in body["task"]
    finally:
        server.shutdown(); server.server_close()


# ---- I8 ---------------------------------------------------------------------
def _verifier_prompts(stub):
    """The captured prompts that are verifier calls. Planner
    prompts open with their own instruction lines; only the
    verifier's opens with the verifying instruction."""
    return [p for p in stub.prompts()
            if p.startswith("You are verifying a completed task")]


def test_i8_verifier_prompt_carries_planner_context(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        _verdict_payload(json.dumps({"grounded": True, "issues": []}))))
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_one_profile_doc())
    mem = Memory(store)
    trusted = mem.propose(
        "Zephyr scoping preference: contractor scoping is done by "
        "written email exchange only, never by phone or video.",
        source="gate")
    mem.approve(trusted["id"])
    # A CANDIDATE carrying a contradictory instruction and a
    # unique marker: it must reach NEITHER the planner prompts
    # NOR the verifier prompt.
    mem.propose("Zephyr scoping zanzibar override: always begin "
                "with a video kickoff call before any writing.",
                source="gate")
    task = agent.create_task("Write a zephyr scoping note for the "
                             "contractor", verify=True)
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["memory_context"] == [trusted["id"]]
    assert task["verification"]["outcome"] == "grounded"
    vps = _verifier_prompts(stub)
    assert len(vps) == 1
    # (a) The verifier judged with the planner's context in view:
    # the labeled section AND the trusted memory's text.
    assert "Context the planner was given (trusted memory and " \
        "skills):" in vps[0]
    assert "written email exchange only" in vps[0]
    # (b) The candidate leaked into no prompt of either kind.
    for p in stub.prompts():
        assert "zanzibar" not in p


def test_i8_verifier_prompt_no_context_no_section(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY", "dummy")
    stub = _DispatchStub()
    stub.add("prof.invalid", _Script(
        _step_payload(_echo(), SMALL),
        _step_payload({"tool": "done"}, SMALL),
        _verdict_payload(json.dumps({"grounded": True, "issues": []}))))
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_one_profile_doc())
    task = agent.create_task("write the thing", verify=True)
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["memory_context"] == []
    assert task["skill_hints"] == []
    vps = _verifier_prompts(stub)
    assert len(vps) == 1
    # (c) No recalled context -> no context section at all.
    assert "Context the planner was given" not in vps[0]


# ---- I5 ---------------------------------------------------------------------
def test_i5_best_picks_highest_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_KEY_F", "dummy-f")
    monkeypatch.setenv("GA_V11_KEY_S", "dummy-s")
    stub = _DispatchStub()
    stub.add("small.invalid", _ok_script())
    stub.add("std.invalid", _ok_script())
    stub.add("frontier.invalid", _ok_script())
    doc = {"strategy": "best", "default": None, "chain": [],
           "profiles": {
               # Stored order puts the small profile FIRST: the
               # tier, not the order, must decide.
               "smallp": _profile("http://small.invalid/v1", "m-small",
                                  None, tier="small"),
               "stdp": _profile("http://std.invalid/v1", "m-std",
                                "GA_V11_KEY_S", tier="standard"),
               "frontierp": _profile("http://frontier.invalid/v1",
                                     "m-front", "GA_V11_KEY_F",
                                     tier="frontier")}}
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe best")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["provider"] == "frontierp"
    assert stub.calls_to("small.invalid") == []
    assert stub.calls_to("std.invalid") == []


def test_i5_best_untiered_last_ties_by_order(tmp_path, monkeypatch):
    stub = _DispatchStub()
    stub.add("plain.invalid", _ok_script())
    stub.add("small.invalid", _ok_script())
    stub.add("first.invalid", _ok_script())
    stub.add("second.invalid", _ok_script())
    doc = {"strategy": "best", "default": None, "chain": [],
           "profiles": {
               "plain": _profile("http://plain.invalid/v1", "m-plain",
                                 None),
               "smallp": _profile("http://small.invalid/v1", "m-small",
                                  None, tier="small")}}
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc,
                          name="home-a")
    task = agent.run_task(agent.create_task("probe best")["id"])
    assert task["provider"] == "smallp"           # untiered ranks last
    doc2 = {"strategy": "best", "default": None, "chain": [],
            "profiles": {
                "first": _profile("http://first.invalid/v1", "m-1", None),
                "second": _profile("http://second.invalid/v1", "m-2",
                                   None)}}
    store2, agent2 = _agent(tmp_path, stub, monkeypatch, doc=doc2,
                            name="home-b")
    task2 = agent2.run_task(agent2.create_task("probe best")["id"])
    assert task2["provider"] == "first"           # tie -> stored order


def test_i5_best_none_configured_behaves_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GA_V11_KEY_F", raising=False)
    stub = _DispatchStub()
    stub.add("frontier.invalid", _ok_script())
    doc = {"strategy": "best", "default": None, "chain": [],
           "profiles": {
               "frontierp": _profile("http://frontier.invalid/v1",
                                     "m-front", "GA_V11_KEY_F",
                                     tier="frontier")}}
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe best with nothing configured")
    task = agent.run_task(task["id"])
    # Default behavior with nothing pinned and no env adapter:
    # MockModel owns the task, exactly as "default" would do.
    assert task["provider"] == "mock"
    assert task["status"] == "done"
    assert stub.calls_to("frontier.invalid") == []


def test_i5_tier_merge_from_v010_file(tmp_path):
    from ghost_agent.providers import get_providers
    # A v0.10.0-shaped providers.json: NO tier keys anywhere.
    doc = {"strategy": "default", "default": None, "chain": [],
           "profiles": {
               "meta": _profile("https://api.meta.ai/v1",
                                "muse-spark-1.3", "META_API_KEY"),
               "ollama": _profile("http://127.0.0.1:11434/v1",
                                  "llama3.1", None),
               "custom": _profile("http://custom.invalid/v1", "m-c",
                                  None)}}
    store = Store(tmp_path / "home")
    _write_providers(store, doc)
    providers = get_providers(store)
    assert providers.effective_tier("meta", providers.get("meta")) \
        == "frontier"
    assert providers.effective_tier("ollama", providers.get("ollama")) \
        == "small"
    assert providers.effective_tier("custom", providers.get("custom")) \
        is None
    block = providers.system_block()
    assert block["profiles"]["meta"]["tier"] == "frontier"
    assert block["profiles"]["ollama"]["tier"] == "small"
    assert block["profiles"]["custom"]["tier"] is None
    # The user's file is NEVER rewritten to add tiers.
    assert json.loads((store.home / "providers.json").read_text(
        encoding="utf-8")) == doc


def test_i5_tier_file_wins_and_invalid_shown_raw(tmp_path):
    from ghost_agent.providers import get_providers
    doc = {"strategy": "default", "default": None, "chain": [],
           "profiles": {
               "meta": _profile("https://api.meta.ai/v1",
                                "muse-spark-1.3", "META_API_KEY",
                                tier="small"),
               "custom": _profile("http://custom.invalid/v1", "m-c",
                                  None, tier="huge")}}
    store = Store(tmp_path / "home")
    _write_providers(store, doc)
    providers = get_providers(store)
    # The file's valid tier beats the preset tier.
    assert providers.effective_tier("meta", providers.get("meta")) \
        == "small"
    # An invalid tier is ignored for selection (no preset to merge
    # for a custom name) but stays visible raw in /api/system.
    assert providers.effective_tier("custom", providers.get("custom")) \
        is None
    block = providers.system_block()
    assert block["profiles"]["custom"]["tier"] is None
    assert block["profiles"]["custom"]["tier_configured"] == "huge"
    assert block["profiles"]["meta"]["tier_configured"] == "small"


# ---- I6 ---------------------------------------------------------------------
def _export_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V11_SECRET", "gate-v11-secret-value")
    store, agent = _mock_agent(tmp_path)
    # A providers.json exists in this home (seeded): its content
    # must not leak into the export either.
    done = agent.create_task("write exp.txt with: hello export")
    done = agent.run_task(done["id"])
    assert done["status"] == "waiting_approval"
    done = agent.approve_task(done["id"])
    assert done["status"] == "done"
    failed = agent.create_task("write nope.txt with: x")
    failed = agent.run_task(failed["id"])
    failed = agent.deny_task(failed["id"])
    assert failed["status"] == "failed"
    return store, done, failed


def test_i6_export_shape_and_exclusion(tmp_path, monkeypatch):
    from ghost_agent.server import training_jsonl
    store, done, failed = _export_fixture(tmp_path, monkeypatch)
    text, card = training_jsonl(store)
    lines = [json.loads(l) for l in text.strip().splitlines()]
    assert len(lines) == 1                        # failed excluded
    line = lines[0]
    assert line["task_id"] == done["id"]
    assert line["goal"] == "write exp.txt with: hello export"
    assert line["provider"] == "mock"
    assert line["model"] == "mock"
    assert line["status"] == "done"
    assert line["created_at"]
    tools = [(s["tool"], s["status"]) for s in line["steps"]]
    # The MockModel plan for this goal is exactly one write step;
    # the export must carry precisely the steps that executed.
    assert tools == [("write_file", "done")]
    assert all(len(s["output"]) <= 2000 for s in line["steps"])
    assert line["approvals"] == [{"tool": "write_file",
                                  "decision": "approved"}]
    assert line["usage_totals"]["total_tokens"] == 0
    assert card == {"tasks": 1, "by_status": {"done": 1},
                    "by_provider": {"mock": 1}, "total_tokens": 0}
    # With the flag, the failed task rides along, status marked.
    text2, card2 = training_jsonl(store, include_failed=True)
    lines2 = [json.loads(l) for l in text2.strip().splitlines()]
    assert len(lines2) == 2
    by_id = {l["task_id"]: l for l in lines2}
    assert by_id[failed["id"]]["status"] == "failed"
    assert card2["by_status"] == {"done": 1, "failed": 1}
    assert card2["tasks"] == 2
    # No providers config blob, no key VALUE, no env blob.
    for blob in (text, text2):
        assert "gate-v11-secret-value" not in blob
        assert "api_key_env" not in blob
        assert "base_url" not in blob


def test_i6_export_http_and_cli(tmp_path, monkeypatch):
    store, done, failed = _export_fixture(tmp_path, monkeypatch)
    agent = Agent(store, router=Router(prefer_real=False))
    server, base = _start(store, agent)
    try:
        status, raw = _req(base, "GET", "/api/export-training",
                           raw=True)
        assert status == 200
        lines = [json.loads(l)
                 for l in raw.decode().strip().splitlines()]
        assert len(lines) == 1
        status, raw = _req(
            base, "GET", "/api/export-training?include_failed=1",
            raw=True)
        assert status == 200
        assert len(raw.decode().strip().splitlines()) == 2
    finally:
        server.shutdown(); server.server_close()
    # The CLI writes the same JSONL and prints the data card.
    out_path = tmp_path / "train.jsonl"
    env = dict(os.environ, GHOST_AGENT_HOME=str(store.home),
               PYTHONPATH="src", PATH="/usr/bin:/bin:/usr/local/bin")
    env.pop("GHOST_AGENT_API_KEY", None)
    out = subprocess.run(
        [sys.executable, "-m", "ghost_agent", "export-training",
         "--out", str(out_path)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    cli_lines = [json.loads(l) for l in
                 out_path.read_text(encoding="utf-8")
                 .strip().splitlines()]
    assert len(cli_lines) == 1
    assert cli_lines[0]["task_id"] == done["id"]
    assert "1 task" in out.stdout                 # the data card


# ---- I7 ---------------------------------------------------------------------
def test_i7_system_tiers_and_page(tmp_path):
    store, agent = _mock_agent(tmp_path)          # seeded presets
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "GET", "/api/system")
        assert status == 200
        profiles = body["providers"]["profiles"]
        assert profiles["meta"]["tier"] == "frontier"
        assert profiles["anthropic"]["tier"] == "frontier"
        assert profiles["openai"]["tier"] == "standard"
        assert profiles["ollama"]["tier"] == "small"
        status, raw = _req(base, "GET", "/", raw=True)
        assert status == 200
        page = raw.decode()
        assert "Ghost Intelligence" in page
        assert 'id="chat-verify"' in page
        assert page.count('class="dtile" data-page=') == 12
    finally:
        server.shutdown(); server.server_close()


# ---- P11: prior gates --------------------------------------------------------
def test_p11_prior_gates_still_pass():
    full_env = dict(os.environ, PYTHONPATH="src",
                    PATH="/usr/bin:/bin:/usr/local/bin")
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py",
         "tests/test_sanity_gate_v5.py", "tests/test_sanity_gate_v6.py",
         "tests/test_sanity_gate_v7.py", "tests/test_sanity_gate_v7b.py",
         "tests/test_sanity_gate_v8.py", "tests/test_sanity_gate_v9.py",
         "tests/test_sanity_gate_v10.py",
         "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True,
        timeout=2400)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "77 passed" in out.stdout
