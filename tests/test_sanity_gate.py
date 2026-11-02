"""KNOWN-ANSWER SANITY GATE — run this file FIRST, on its own.

K1 read_file on an existing fixture -> governance allow, execution succeeds.
K2 write_file with NO approval -> blocked/waiting, file ABSENT afterward.
K3 write_file WITH approval -> executes, exact content, trail record
   exists and precedes execution (trail ts_ns <= file mtime_ns).
K4 trail-write fault injection -> action does NOT execute.
"""
import os
import shutil

from ghost_agent import governance


def test_k1_read_existing_fixture_allowed(agent, store):
    fixture = store.workspace / "fixture.txt"
    fixture.write_text("known answer 42", encoding="utf-8")
    assert governance.classify("read_file").action == "allow"
    task = agent.create_task("read fixture.txt")
    task = agent.run_task(task["id"])
    assert task["status"] == "done"
    assert "known answer 42" in task["steps"][0]["result"]["output"]


def test_k2_write_without_approval_blocked_and_absent(agent, store):
    target = store.workspace / "blocked.txt"
    assert not target.exists()
    task = agent.create_task("write blocked.txt with: should not land")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    assert not target.exists(), "write executed without approval"


def test_k3_write_with_approval_executes_and_trail_precedes(agent, store):
    target = store.workspace / "approved.txt"
    task = agent.create_task("write approved.txt with: exact-content-123")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    task = agent.approve_task(task["id"])
    assert task["status"] == "done"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "exact-content-123"
    trail = [e for e in store.read_trail(task_id=task["id"])
             if e["type"] == "tool_execute" and e["tool"] == "write_file"]
    assert trail, "no tool_execute trail record for write_file"
    file_mtime_ns = os.stat(target).st_mtime_ns
    assert trail[0]["ts_ns"] <= file_mtime_ns, "trail record did not precede execution"


def test_k4_trail_fault_blocks_execution(agent, store):
    target = store.workspace / "never.txt"
    task = agent.create_task("write never.txt with: must not land")
    task = agent.run_task(task["id"])
    assert task["status"] == "waiting_approval"
    # Fault injection: make the trail target unwritable (a directory
    # where the JSONL file belongs).
    trail = store.trail_path
    if trail.exists():
        trail.unlink()
    trail.mkdir()
    try:
        task = agent.approve_task(task["id"])
    finally:
        shutil.rmtree(trail)
    assert task["status"] == "failed"
    assert "trail" in (task.get("error") or "").lower()
    assert not target.exists(), "action executed despite trail write failure"
