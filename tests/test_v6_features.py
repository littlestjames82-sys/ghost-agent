"""v0.6.0 feature unit tests (the gate covers the end-to-end known
answers; these pin the smaller contracts).

Includes the live-run follow-up (item 5): the OpenAI-compatible
planner prompt must steer the model away from shell extraction
shortcuts — composed content via write_file, shell only when the
goal asks for a command, reads before dependent writes — and plan
parsing must be unchanged.
"""
import json

import pytest

from ghost_agent.governance import classify
from ghost_agent.models import (MockModel, OpenAICompatibleModel,
                                build_planner_prompt, estimate_cost_usd,
                                usage_record)
from ghost_agent.server import import_snapshot
from ghost_agent.tools import Tools


# ---- item 5: planner prompt guidance ------------------------------------
def test_planner_prompt_contains_live_run_guidance():
    prompt = build_planner_prompt("summarize README.md into summary.md")
    # (a) prefer write_file with self-composed content for writing tasks
    assert "prefer write_file with content you compose yourself" in prompt
    assert "do not use shell to extract" in prompt
    # (b) shell only when the goal explicitly asks for a command
    assert "Use shell only when the goal explicitly asks" in prompt
    # (c) reads of sources come before any write that depends on them
    assert "comes before any write that depends on its content" in prompt
    # the goal itself is still appended, unchanged
    assert prompt.endswith("Goal: summarize README.md into summary.md")


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_plan_parsing_unchanged_with_guidance_prompt(monkeypatch):
    plan = [{"tool": "read_file", "args": {"path": "README.md"}},
            {"tool": "write_file",
             "args": {"path": "summary.md", "content": "composed"},
             "verify": {"type": "ok"}}]
    payload = {"model": "stub-plan-model",
               "choices": [{"message": {
                   "content": "Plan:\n" + json.dumps(plan) + "\n"}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                         "total_tokens": 15}}
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _Resp(payload)

    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen",
                        fake_urlopen)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", "unit-test-key")
    model = OpenAICompatibleModel(base_url="http://stub.invalid/v1",
                                  model="stub-plan-model")
    steps = model.plan("summarize README.md into summary.md")
    # Parsing behavior is exactly the pre-guidance behavior...
    assert [s["tool"] for s in steps] == ["read_file", "write_file"]
    assert steps[0]["verify"] == {"type": "ok"}        # defaults filled
    assert steps[1]["args"]["content"] == "composed"
    # ...while the request that went out carries the new guidance.
    sent_prompt = seen["body"]["messages"][0]["content"]
    assert "content you compose yourself" in sent_prompt
    assert model.last_usage["total_tokens"] == 15


# ---- item 1: pricing + usage records -------------------------------------
def test_estimate_cost_known_prefixes_only():
    assert estimate_cost_usd("gpt-4o-mini", 1_000_000, 0) == \
        pytest.approx(0.15)
    assert estimate_cost_usd("gpt-4o-mini-2024-07-18", 0, 1_000_000) == \
        pytest.approx(0.60)
    assert estimate_cost_usd("gpt-4o", 1_000_000, 1_000_000) == \
        pytest.approx(12.50)
    # Longest prefix wins: gpt-4o-mini is not priced as gpt-4o.
    assert estimate_cost_usd("gpt-4o-mini", 1_000_000, 0) != \
        pytest.approx(2.50)
    # Unknown models: None, never a guessed number.
    assert estimate_cost_usd("stub-model-x", 120, 30) is None
    assert estimate_cost_usd(None, 1, 1) is None


def test_usage_record_shapes():
    rec = usage_record("stub-model-x", "provider",
                       {"prompt_tokens": 120, "completion_tokens": 30,
                        "total_tokens": 150})
    assert rec["total_tokens"] == 150
    assert rec["estimated_cost_usd"] is None
    assert rec["cost_status"] == "unknown"
    rec = usage_record("mock", "mock", {"prompt_tokens": 0,
                                        "completion_tokens": 0,
                                        "total_tokens": 0})
    assert rec["source"] == "mock" and rec["estimated_cost_usd"] == 0.0
    rec = usage_record("gpt-4o-mini", "provider", {"prompt_tokens": 7})
    assert rec["total_tokens"] == 7          # total derived when absent
    assert rec["cost_status"] == "estimated"


# ---- item 3: tools + planner patterns ------------------------------------
def test_new_tool_governance_classes():
    assert classify("search_files").action == "allow"
    assert classify("append_file").action == "approval"
    assert classify("write_file").action == "approval"


def test_mock_planner_new_patterns():
    m = MockModel()
    steps = m.plan("find needle in docs")
    assert [s["tool"] for s in steps] == ["search_files"]
    assert steps[0]["args"] == {"query": "needle", "path": "docs"}
    steps = m.plan("search docs for needle")
    assert [s["tool"] for s in steps] == ["search_files"]
    assert steps[0]["args"] == {"query": "needle", "path": "docs"}
    steps = m.plan("summarize notes.md")
    assert [s["tool"] for s in steps] == ["read_file", "echo_note"]
    assert "MockModel plan" in steps[1]["args"]["text"]
    steps = m.plan("append hello world to log.txt")
    assert [s["tool"] for s in steps] == ["append_file"]
    assert steps[0]["args"] == {"path": "log.txt", "content": "hello world"}
    # Old patterns are untouched.
    assert [s["tool"] for s in m.plan("demo")] == \
        ["echo_note", "write_file", "read_file"]
    assert m.plan("write a.txt with: hi")[0]["tool"] == "write_file"


def test_append_file_tool_is_raw_append(tmp_path):
    tools = Tools(tmp_path / "ws")
    (tmp_path / "ws" / "f.txt").write_text("one\n", encoding="utf-8")
    out = tools.append_file("f.txt", "two")
    assert out["ok"] is True
    assert (tmp_path / "ws" / "f.txt").read_bytes() == b"one\ntwo"


# ---- item 2: import validation -------------------------------------------
def test_import_validation_rejects_wrong_shapes(store):
    for bad in (None, [], "x", {"bogus": True},
                {"tasks": []},                       # missing lists
                {"tasks": [], "approvals": [], "memories_trusted": [],
                 "memories_candidates": [], "skills": [], "schedules": [],
                 "trail": [{"detail": []}]},          # bad trail detail
                {"tasks": [{"id": "t1"}, {"id": "t1"}], "approvals": [],
                 "memories_trusted": [], "memories_candidates": [],
                 "skills": [], "schedules": [], "trail": []}):  # dup ids
        with pytest.raises(ValueError):
            import_snapshot(store, bad)
    # Every rejection left the store completely untouched.
    for coll in ("memories", "skills", "schedules", "tasks",
                 "archived_tasks", "archived_approvals", "archived_trail"):
        assert store.list(coll) == []
