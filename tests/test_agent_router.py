from ghost_agent.agent import Agent
from ghost_agent.models import MockModel, ModelError, Router


class FailingReal:
    name = "failing-real"
    is_mock = False

    def plan(self, goal):
        raise ModelError("provider down (simulated)")


def test_router_mock_without_key(monkeypatch):
    monkeypatch.delenv("GHOST_AGENT_API_KEY", raising=False)
    r = Router()
    out = r.plan("read a.txt")
    assert out["model"] == "mock" and out["fallback_reason"] is None


def test_router_fallback_records_reason(store):
    r = Router(prefer_real=False)
    r.real = FailingReal()
    r.prefer_real = True
    out = r.plan("read a.txt")
    assert out["model"] == "mock"
    assert "provider down" in out["fallback_reason"]
    # And the agent records the fallback in the trail.
    agent = Agent(store, router=r)
    task = agent.create_task("read a.txt")
    assert task["fallback_reason"] and "provider down" in task["fallback_reason"]
    types = [e["type"] for e in store.read_trail(task_id=task["id"])]
    assert "model_fallback" in types


def test_mock_plans_are_deterministic():
    m = MockModel()
    assert m.plan("write a.txt with: hi") == m.plan("write a.txt with: hi")
    demo = m.plan("demo")
    assert [s["tool"] for s in demo] == ["echo_note", "write_file", "read_file"]


def test_loop_happy_path_read_only(agent, store):
    (store.workspace / "note.txt").write_text("hello world", encoding="utf-8")
    task = agent.create_task("read note.txt")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert task["steps"][0]["status"] == "done"
    # A candidate skill is proposed, and it is untrusted.
    assert task["skill_id"]
    skill = store.get("skills", task["skill_id"])
    assert skill["status"] == "candidate" and skill["trusted"] is False


def test_loop_approval_path(agent, store):
    task = agent.create_task("write out.txt with: landed")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    assert not (store.workspace / "out.txt").exists()
    assert len(agent.pending_approvals()) == 1
    task = agent.approve_task(task["id"])
    assert task["status"] == "done"
    assert (store.workspace / "out.txt").read_text() == "landed"


def test_loop_deny_stops_run(agent, store):
    task = agent.create_task("write nope.txt with: x")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    task = agent.deny_task(task["id"])
    assert task["status"] == "failed"
    assert task["steps"][0]["status"] == "failed"
    assert not (store.workspace / "nope.txt").exists()
    assert agent.pending_approvals() == []


def test_loop_unknown_tool_denied(store):
    agent = Agent(store, router=Router(prefer_real=False))
    task = store.put("tasks", {
        "id": "tsk_manual", "title": "manual", "goal": "manual", "status": "pending",
        "current_step": 0, "model": "mock", "fallback_reason": None, "skill_id": None,
        "error": None,
        "steps": [{"tool": "rm_rf", "args": {}, "verify": {"type": "ok"},
                   "status": "pending", "approved": False, "result": None, "error": None}],
    })
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"
    assert "deny-by-default" in task["error"]


def test_tool_sandbox_escape_refused(agent, store):
    task = agent.create_task("read ../outside.txt")
    task = agent.run_task(task["id"])
    assert task["status"] == "failed"


def test_skill_promotion(store, agent):
    task = agent.create_task("read missing-but-ok.txt")  # echo fallback? no: read fails
    # Use a guaranteed-success read instead:
    (store.workspace / "s.txt").write_text("s", encoding="utf-8")
    task = agent.create_task("read s.txt")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    from ghost_agent.skills import Skills
    sk = Skills(store)
    promoted = sk.promote(task["skill_id"])
    assert promoted["trusted"] is True and promoted["status"] == "trusted"
