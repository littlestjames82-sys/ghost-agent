"""Ghost Agent v10 SANITY GATE — known answers, run FIRST.

v0.10.0 is "other models and routers": named provider profiles,
router strategies, and a native Anthropic adapter.

1. PROVIDER PROFILES live in providers.json in the agent home
   ({strategy, default, chain, profiles:{name:{base_url, model,
   api_key_env, kind}}}). The home file wins; on first run it is
   seeded with presets (meta, openai, openrouter, fireworks,
   groq, together, gemini, ollama, lmstudio, anthropic). Keys are
   referenced by env-var NAME only — a key value is never written
   to providers.json, the store, or the trail. Loading is
   defensive: a malformed file yields a visible load_error and
   the legacy env behavior, never a crash, and the broken file
   is never overwritten. A profile is "configured" when its
   api_key_env is null (keyless local) or that env var is set.
2. BACK-COMPAT: with no usable providers config and the legacy
   env (GHOST_AGENT_API_KEY / _BASE_URL / _MODEL) present,
   behavior is exactly v0.9.0 — an implicit "env" profile, and
   Router(prefer_real=...) / router.real keep working.
3. ANTHROPIC ADAPTER (kind "anthropic"): POST {base}/v1/messages
   with x-api-key + anthropic-version: 2023-06-01, body {model,
   max_tokens: 2048, messages:[user prompt]}; text blocks are
   concatenated; usage {input_tokens, output_tokens} normalizes
   into the provider usage shape; an empty content list is the
   "provider returned an empty response" ModelError.
4. STRATEGIES (providers.json "strategy", default "default"):
   "default" serves the default profile (first-call MockModel
   fallback as always); "chain" tries the chain's configured
   profiles in order at the task's FIRST call only — first
   success owns the task, failures are recorded in
   fallback_reason, all-fail is a MockModel fallback with a
   combined reason; "cheapest" picks the lowest-priced (input+
   output per-million sum) configured profile whose model has
   known pricing, else behaves as "default". Mid-task failures
   NEVER switch brains (v7 K5 semantics).
5. PER-TASK OVERRIDE + ATTRIBUTION: create_task(profile=...)
   and POST /api/tasks {"profile": ...}; an unknown or
   unconfigured profile is an honest creation error (ValueError
   -> HTTP 400), never a silent substitution. Task records gain
   "provider" (profile name, "env", or "mock") next to "model";
   model_usage trail events carry the provider; /api/analytics
   usage gains a by_provider rollup; /api/system gains a
   "providers" block with configured/key_present booleans —
   key values never appear.
6. PRICING: only claude-sonnet-4-5 ($3/$15) and
   claude-haiku-4-5 ($1/$5) are added; everything else unpriced
   stays cost "unknown".

P1 seed + home-wins + malformed fallback to legacy env.
P2 default strategy serves the NAMED default profile.
P3 chain: failover records the failure; all-fail -> mock with a
   combined reason; unconfigured chain entries are skipped.
P4 cheapest picks the known-cheapest configured profile;
   unpriced-only configured set behaves as "default".
P5 override honored; unknown/unconfigured -> ValueError; the
   HTTP path surfaces it as 400.
P6 anthropic request shape, text-block parsing, usage
   normalization + estimated cost; empty content -> the empty
   response ModelError (mock fallback with that reason).
P7 keyless ollama profile attempts with NO Authorization header.
P8 mid-task chain failure does NOT switch profiles.
P9 /api/system providers payload contains NO dummy key value
   while key_present is true for the configured dummy profile.
P10 Back-compat: gates v1..v9 (58 tests) pass in the same run
   (subprocess, as in prior gates).

Feature assertions live inside the tests (and feature imports
are lazy) so the baseline run on v0.9.0 code fails each case
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
from ghost_agent.models import OpenAICompatibleModel, Router
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


def _profile(base_url, model, key_env, kind="openai-compatible"):
    return {"base_url": base_url, "model": model,
            "api_key_env": key_env, "kind": kind}


def _agent(tmp_path, stub, monkeypatch, doc=None, name="home"):
    """An Agent over a fresh store whose home holds the fixture
    providers.json (when given), with urlopen stubbed and a
    mock-only-by-default Router (providers do the routing)."""
    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen", stub)
    store = Store(tmp_path / name)
    if doc is not None:
        _write_providers(store, doc)
    return store, Agent(store, router=Router(prefer_real=False))


def _two_profiles(strategy="default", default="prof-b", chain=None):
    return {
        "strategy": strategy,
        "default": default,
        "chain": chain if chain is not None else [],
        "profiles": {
            "prof-a": _profile("http://prof-a.invalid/v1", "model-a",
                               "GA_V10_KEY_A"),
            "prof-b": _profile("http://prof-b.invalid/v1", "model-b",
                               "GA_V10_KEY_B"),
        },
    }


def _ok_script(model="stub-model"):
    return _Script(_step_payload(_echo(), SMALL, model=model),
                   _step_payload({"tool": "done"}, SMALL, model=model))


# ---- P1 ---------------------------------------------------------------------
def test_p1_seed_first_run(tmp_path, monkeypatch):
    from ghost_agent.providers import get_providers
    for var in ("META_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    store = Store(tmp_path / "home")
    providers = get_providers(store)
    seeded = store.home / "providers.json"
    assert seeded.exists()                       # the user's editable copy
    doc = json.loads(seeded.read_text(encoding="utf-8"))
    assert doc["strategy"] == "default"
    assert doc["default"] is None                # nothing pinned: legacy env rules
    assert doc["profiles"]["meta"]["model"] == "muse-spark-1.3"
    assert doc["profiles"]["meta"]["base_url"] == "https://api.meta.ai/v1"
    assert doc["profiles"]["anthropic"]["kind"] == "anthropic"
    assert providers.load_error is None
    assert providers.get("ollama")["api_key_env"] is None
    assert providers.configured(providers.get("ollama")) is True   # keyless
    assert providers.configured(providers.get("meta")) is False    # no key set


def test_p1_home_file_wins(tmp_path):
    from ghost_agent.providers import get_providers
    store = Store(tmp_path / "home")
    _write_providers(store, {
        "strategy": "default", "default": "mine", "chain": [],
        "profiles": {"mine": _profile("http://mine.invalid/v1", "m1",
                                      "GA_V10_KEY_MINE")},
    })
    providers = get_providers(store)
    assert providers.get("mine") is not None
    assert providers.get("meta") is None         # presets are NOT merged in
    assert providers.default == "mine"
    assert providers.load_error is None


def test_p1_malformed_falls_back_to_legacy(tmp_path, monkeypatch):
    from ghost_agent.providers import get_providers
    store = Store(tmp_path / "home")
    broken = "{ this is not json"
    (store.home / "providers.json").write_text(broken, encoding="utf-8")
    providers = get_providers(store)
    assert providers.load_error                        # visible, not silent
    assert providers.profiles == {}

    stub = _DispatchStub()
    stub.add("stub.invalid", _ok_script())
    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen", stub)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", "gate-v10-dummy-key")
    router = Router(prefer_real=True)
    router.real = OpenAICompatibleModel(base_url="http://stub.invalid/v1",
                                        model="stub-model")
    agent = Agent(store, router=router)
    task = agent.create_task("probe the legacy path")
    task = agent.run_task(task["id"])
    # Exactly v0.9.0 behavior: the env adapter owns the task.
    assert task["provider"] == "env"
    assert task["model"] == "openai-compatible"
    assert task["status"] == "done"
    # The broken home file is the user's file: never overwritten.
    assert (store.home / "providers.json").read_text(
        encoding="utf-8") == broken


# ---- P2 ---------------------------------------------------------------------
def test_p2_default_strategy_serves_named_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    a = stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_two_profiles(default="prof-b"))
    task = agent.create_task("probe the default")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["provider"] == "prof-b"
    assert task["model"] == "openai-compatible"
    assert task["fallback_reason"] is None
    assert len(stub.calls_to("prof-b.invalid")) == 2
    assert a.calls == 0 and stub.calls_to("prof-a.invalid") == []


# ---- P3 ---------------------------------------------------------------------
def test_p3_chain_failover_records_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    stub.add("prof-a.invalid",
             _Script(urllib.error.URLError("connection refused")))
    stub.add("prof-b.invalid", _ok_script())
    doc = _two_profiles(strategy="chain", default=None,
                        chain=["prof-a", "prof-b"])
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe the chain")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["provider"] == "prof-b"            # first success owns it
    assert "prof-a" in (task["fallback_reason"] or "")
    assert len(stub.calls_to("prof-b.invalid")) == 2


def test_p3_chain_all_fail_mock_with_combined_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    stub.add("prof-a.invalid",
             _Script(urllib.error.URLError("a is down")))
    stub.add("prof-b.invalid",
             _Script(urllib.error.URLError("b is down")))
    doc = _two_profiles(strategy="chain", default=None,
                        chain=["prof-a", "prof-b"])
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("read note.txt")
    assert task["provider"] == "mock"
    assert task["model"] == "mock"
    reason = task["fallback_reason"] or ""
    assert "prof-a" in reason and "prof-b" in reason


def test_p3_chain_skips_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("GA_V10_KEY_A", raising=False)   # prof-a unconfigured
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    a = stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    doc = _two_profiles(strategy="chain", default=None,
                        chain=["prof-a", "prof-b"])
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe the chain")
    task = agent.run_task(task["id"])
    assert task["provider"] == "prof-b"
    assert task["fallback_reason"] is None         # a skip is not a failure
    assert a.calls == 0


# ---- P4 ---------------------------------------------------------------------
def test_p4_cheapest_picks_known_cheapest(tmp_path, monkeypatch):
    for var in ("GA_V10_KEY_A", "GA_V10_KEY_B", "GA_V10_KEY_C"):
        monkeypatch.setenv(var, "dummy")
    stub = _DispatchStub()
    stub.add("pricey.invalid", _ok_script())
    cheap = stub.add("cheap.invalid", _ok_script())
    stub.add("mystery.invalid", _ok_script())
    doc = {
        "strategy": "cheapest",
        "default": "pricey",                       # cheapest must override this
        "chain": [],
        "profiles": {
            "pricey": _profile("http://pricey.invalid/v1", "muse-spark-1.3",
                               "GA_V10_KEY_A"),
            "cheap": _profile("http://cheap.invalid/v1", "gpt-4o-mini",
                              "GA_V10_KEY_B"),
            "mystery": _profile("http://mystery.invalid/v1",
                                "totally-unpriced-9000", "GA_V10_KEY_C"),
        },
    }
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe cheapest")
    task = agent.run_task(task["id"])
    # gpt-4o-mini ($0.15+$0.60) beats muse-spark ($1.25+$4.25);
    # the unpriced model is not a candidate at all.
    assert task["provider"] == "cheap"
    assert cheap.calls == 2
    assert stub.calls_to("pricey.invalid") == []
    assert stub.calls_to("mystery.invalid") == []


def test_p4_cheapest_unpriced_only_behaves_as_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GA_V10_KEY_B", raising=False)  # priced profile unconfigured
    monkeypatch.setenv("GA_V10_KEY_C", "dummy")
    stub = _DispatchStub()
    stub.add("mystery.invalid", _ok_script())
    stub.add("cheap.invalid", _ok_script())
    doc = {
        "strategy": "cheapest",
        "default": "mystery",
        "chain": [],
        "profiles": {
            "cheap": _profile("http://cheap.invalid/v1", "gpt-4o-mini",
                              "GA_V10_KEY_B"),
            "mystery": _profile("http://mystery.invalid/v1",
                                "totally-unpriced-9000", "GA_V10_KEY_C"),
        },
    }
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe cheapest fallback")
    task = agent.run_task(task["id"])
    # No CONFIGURED profile is priced -> "default" behavior: the
    # named default (unpriced) serves.
    assert task["provider"] == "mystery"
    assert stub.calls_to("cheap.invalid") == []


# ---- P5 ---------------------------------------------------------------------
def test_p5_override_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    a = stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_two_profiles(default="prof-a"))
    task = agent.create_task("probe override", profile="prof-b")
    task = agent.run_task(task["id"])
    assert task["provider"] == "prof-b"
    assert a.calls == 0


def test_p5_override_errors_are_honest(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.delenv("GA_V10_KEY_B", raising=False)  # prof-b unconfigured
    stub = _DispatchStub()
    stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_two_profiles(default="prof-a"))
    with pytest.raises(ValueError):
        agent.create_task("probe", profile="no-such-profile")
    with pytest.raises(ValueError):
        agent.create_task("probe", profile="prof-b")   # exists, unconfigured
    assert store.list("tasks") == []                   # nothing was created


def _start(store, agent):
    from ghost_agent.server import make_server
    server = make_server("127.0.0.1", 0, store=store, agent=agent,
                         token=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _req(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with REAL_URLOPEN(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_p5_http_profile_400_and_201(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_two_profiles(default="prof-a"))
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "POST", "/api/tasks",
                            {"goal": "probe", "profile": "no-such-profile"})
        assert status == 400 and "error" in body
        status, body = _req(base, "POST", "/api/tasks",
                            {"goal": "probe override", "profile": "prof-b"})
        assert status == 201
        assert body["task"]["provider"] == "prof-b"
    finally:
        server.shutdown(); server.server_close()


# ---- P6 ---------------------------------------------------------------------
def _anthropic_payload(text, input_tokens=100, output_tokens=50,
                       model="claude-sonnet-4-5"):
    return {"model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens,
                      "output_tokens": output_tokens}}


def _claude_doc():
    return {
        "strategy": "default", "default": "claude", "chain": [],
        "profiles": {
            "claude": _profile("http://anthropic.invalid",
                               "claude-sonnet-4-5", "GA_V10_ANTHROPIC_KEY",
                               kind="anthropic"),
        },
    }


def test_p6_anthropic_shape_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_ANTHROPIC_KEY", "dummy-anthropic-key-v10")
    stub = _DispatchStub()
    stub.add("anthropic.invalid", _Script(
        _anthropic_payload(json.dumps(_echo("from claude"))),
        _anthropic_payload(json.dumps({"tool": "done"}),
                           input_tokens=10, output_tokens=5)))
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=_claude_doc())
    task = agent.create_task("probe anthropic")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["provider"] == "claude"
    assert task["model"] == "anthropic"

    first = stub.records[0]
    assert first["url"] == "http://anthropic.invalid/v1/messages"
    assert first["headers"].get("x-api-key") == "dummy-anthropic-key-v10"
    assert first["headers"].get("anthropic-version") == "2023-06-01"
    assert first["body"]["model"] == "claude-sonnet-4-5"
    assert first["body"]["max_tokens"] == 2048
    assert first["body"]["messages"][0]["role"] == "user"

    # usage normalized: (100+10) prompt, (50+5) completion.
    totals = task["usage_totals"]
    assert totals["prompt_tokens"] == 110
    assert totals["completion_tokens"] == 55
    assert totals["total_tokens"] == 165
    entry = task["usage_by_model"]["claude-sonnet-4-5"]
    assert entry["cost_status"] == "estimated"     # priced in v0.10.0
    # (110 x $3.00 + 55 x $15.00) / 1M = $0.001155
    assert abs(entry["estimated_cost_usd"] - 0.001155) < 1e-9
    usage_events = [e for e in store.read_trail(task_id=task["id"])
                    if e["type"] == "model_usage"]
    assert usage_events and \
        usage_events[0]["detail"]["provider"] == "claude"


def test_p6_anthropic_empty_content_is_empty_response(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_ANTHROPIC_KEY", "dummy-anthropic-key-v10")
    stub = _DispatchStub()
    stub.add("anthropic.invalid", _Script(
        {"model": "claude-sonnet-4-5", "content": [],
         "usage": {"input_tokens": 5, "output_tokens": 0}}))
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=_claude_doc())
    task = agent.create_task("read note.txt")
    # First call: the empty response is a provider failure with
    # the shared ModelError reason — MockModel fallback, honestly
    # recorded (the recorded reason names the profile that
    # produced it, like every chain entry: "claude: provider
    # returned an empty response").
    assert task["provider"] == "mock"
    assert "provider returned an empty response" in \
        (task["fallback_reason"] or "")


# ---- P7 ---------------------------------------------------------------------
def test_p7_keyless_profile_attempts_without_authorization(tmp_path,
                                                           monkeypatch):
    monkeypatch.delenv("GHOST_AGENT_API_KEY", raising=False)
    stub = _DispatchStub()
    stub.add("ollama.invalid", _ok_script(model="llama3.1"))
    doc = {
        "strategy": "default", "default": "local", "chain": [],
        "profiles": {
            "local": _profile("http://ollama.invalid/v1", "llama3.1", None),
        },
    }
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe local", profile="local")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["provider"] == "local"
    assert stub.records, "the keyless profile was never attempted"
    assert "authorization" not in stub.records[0]["headers"]


# ---- P8 ---------------------------------------------------------------------
def test_p8_midtask_chain_failure_never_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    stub.add("prof-a.invalid", _Script(
        _step_payload(_echo(), SMALL),
        urllib.error.URLError("a died mid-task")))
    b = stub.add("prof-b.invalid", _ok_script())
    doc = _two_profiles(strategy="chain", default=None,
                        chain=["prof-a", "prof-b"])
    store, agent = _agent(tmp_path, stub, monkeypatch, doc=doc)
    task = agent.create_task("probe midtask")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert "model call failed" in (task["error"] or "")
    assert task["provider"] == "prof-a"            # ownership never moved
    assert b.calls == 0                            # prof-b was never tried


# ---- P9 ---------------------------------------------------------------------
def test_p9_system_block_never_leaks_key_values(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_B", "v10-dummy-secret-9f8e7d")
    monkeypatch.delenv("GA_V10_KEY_A", raising=False)
    stub = _DispatchStub()
    stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_two_profiles(default="prof-b"))
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "GET", "/api/system")
        assert status == 200
        raw = json.dumps(body)
        assert "v10-dummy-secret-9f8e7d" not in raw     # the VALUE, nowhere
        block = body["providers"]
        assert block["strategy"] == "default"
        assert block["default"] == "prof-b"
        prof_b = block["profiles"]["prof-b"]
        assert prof_b["key_present"] is True
        assert prof_b["configured"] is True
        assert prof_b["model"] == "model-b"
        prof_a = block["profiles"]["prof-a"]
        assert prof_a["key_present"] is False
        assert prof_a["configured"] is False
    finally:
        server.shutdown(); server.server_close()


def test_p9b_analytics_by_provider_rollup(tmp_path, monkeypatch):
    monkeypatch.setenv("GA_V10_KEY_A", "dummy-a")
    monkeypatch.setenv("GA_V10_KEY_B", "dummy-b")
    stub = _DispatchStub()
    stub.add("prof-a.invalid", _ok_script())
    stub.add("prof-b.invalid", _ok_script())
    store, agent = _agent(tmp_path, stub, monkeypatch,
                          doc=_two_profiles(default="prof-a"))
    t1 = agent.run_task(agent.create_task("probe one")["id"])
    t2 = agent.run_task(
        agent.create_task("probe two", profile="prof-b")["id"])
    assert t1["provider"] == "prof-a" and t2["provider"] == "prof-b"
    server, base = _start(store, agent)
    try:
        status, body = _req(base, "GET", "/api/analytics")
        assert status == 200
        rollup = body["usage"]["by_provider"]
        # Each task is single-provider: its whole usage lands
        # under its owner, exactly.
        assert rollup["prof-a"]["tasks"] == 1
        assert rollup["prof-a"]["total_tokens"] == 30    # 2 calls x 15
        assert rollup["prof-b"]["tasks"] == 1
        assert rollup["prof-b"]["total_tokens"] == 30
        assert rollup["prof-b"]["cost_status"] == "unknown"  # stub model
    finally:
        server.shutdown(); server.server_close()


# ---- P10 --------------------------------------------------------------------
def test_p10_prior_gates_still_pass():
    full_env = dict(os.environ, PYTHONPATH="src",
                    PATH="/usr/bin:/bin:/usr/local/bin")
    out = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_sanity_gate.py", "tests/test_sanity_gate_v2.py",
         "tests/test_sanity_gate_v3.py", "tests/test_sanity_gate_v4.py",
         "tests/test_sanity_gate_v5.py", "tests/test_sanity_gate_v6.py",
         "tests/test_sanity_gate_v7.py", "tests/test_sanity_gate_v7b.py",
         "tests/test_sanity_gate_v8.py", "tests/test_sanity_gate_v9.py",
         "-q"],
        cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=1800)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "58 passed" in out.stdout
