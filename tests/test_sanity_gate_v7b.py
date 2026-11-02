"""Ghost Agent v7b SANITY GATE — live-found fixes from the v0.7.0
10-goal gauntlet (run against Muse Spark after the v7 gate passed):

K1 Empty provider response on the FIRST call: Spark answered one
   gauntlet goal with a null message content and the run died with
   AttributeError inside create_task. An empty/null content must be
   a ModelError: the router falls back to MockModel for the task
   and records the reason — never a crash.
K2 Empty provider response on a LATER call: the task fails
   honestly with the reason in the trail, the model stays the
   provider's, no mid-task brain switch.
K3 Loop-efficiency contract (the gauntlet's three step-cap
   failures burned ~25k tokens each re-reading files): the
   next-step prompt carries the finish-when-done guidance, and the
   history budget constants are at the raised values.
"""
import json

from ghost_agent.agent import (Agent, HISTORY_OUTPUT_CHARS,
                               HISTORY_TOTAL_CHARS)
from ghost_agent.models import (OpenAICompatibleModel, Router,
                                build_next_step_prompt)
from ghost_agent.store import Store


class _StubResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload(content, usage=None):
    return {"model": "stub-loop-model",
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 5,
                               "completion_tokens": 5,
                               "total_tokens": 10}}


class _ScriptedStub:
    def __init__(self, script):
        self.script = script
        self.prompts = []

    @property
    def calls(self):
        return len(self.prompts)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.prompts.append(body["messages"][0]["content"])
        return _StubResponse(self.script(len(self.prompts)))


def _stub_agent(tmp_path, stub, monkeypatch):
    monkeypatch.setattr("ghost_agent.models.urllib.request.urlopen", stub)
    monkeypatch.setenv("GHOST_AGENT_API_KEY", "gate-v7b-dummy-key")
    store = Store(tmp_path / "home")
    router = Router(prefer_real=True)
    router.real = OpenAICompatibleModel(base_url="http://stub.invalid/v1",
                                        model="stub-loop-model")
    return store, Agent(store, router=router)


# ---- K1 -----------------------------------------------------------------
def test_k1_empty_first_call_falls_back_not_crashes(tmp_path, monkeypatch):
    stub = _ScriptedStub(lambda n: _payload(None))
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "note.txt").write_text("hello\n", encoding="utf-8")
    task = agent.create_task("read note.txt")   # must not raise
    task = agent.run_task(task["id"])
    assert task["model"] == "mock"
    assert "empty" in (task["fallback_reason"] or "")


# ---- K2 -----------------------------------------------------------------
def test_k2_empty_later_call_fails_honestly(tmp_path, monkeypatch):
    def script(n):
        if n == 1:
            return _payload(json.dumps(
                {"tool": "read_file", "args": {"path": "note.txt"},
                 "verify": {"type": "ok"}}))
        return _payload(None)

    stub = _ScriptedStub(script)
    store, agent = _stub_agent(tmp_path, stub, monkeypatch)
    (store.workspace / "note.txt").write_text("hello\n", encoding="utf-8")
    task = agent.create_task("read note.txt then keep going")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert "empty" in (task["error"] or "")
    assert task["model"] == "openai-compatible"
    assert task["fallback_reason"] is None
    kinds = [e["type"] for e in store.read_trail(task_id=task["id"])]
    assert "model_call_failed" in kinds


# ---- K3 -----------------------------------------------------------------
def test_k3_loop_efficiency_contract():
    prompt = build_next_step_prompt("write the thing", [])
    assert "do not re-read" in prompt
    assert "done" in prompt
    assert HISTORY_OUTPUT_CHARS >= 2000
    assert HISTORY_TOTAL_CHARS >= 16000


# ---- K4 -----------------------------------------------------------------
def test_k4_history_elides_outputs_never_steps():
    # 12 completed reads with 6k outputs each: far over any budget.
    # Every step must remain visible (tool + args), the oldest
    # outputs elided with the marker, the newest output intact —
    # the gauntlet's cap failures re-read files whose entries had
    # been dropped whole, because the model could not see it had
    # already read them.
    steps = [{"tool": "read_file", "args": {"path": f"f{i}.md"},
              "status": "done",
              "result": {"output": f"content-{i} " + "x" * 6000}}
             for i in range(12)]
    hist = Agent._history({"steps": steps})
    assert len(hist) == 12
    assert all(h["tool"] == "read_file" for h in hist)
    assert hist[0]["args"] == {"path": "f0.md"}
    assert "elided" in hist[0]["output"]
    assert hist[-1]["output"].startswith("content-11")
    total = sum(len(str(e)) for e in hist)
    assert total <= HISTORY_TOTAL_CHARS + len(str(hist[-1]))
