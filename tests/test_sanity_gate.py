"""KNOWN-ANSWER SANITY GATE — run this file FIRST, on its own.

K1 read_file on an existing fixture -> governance allow, execution succeeds.
K2 write_file with NO approval -> blocked/waiting, file ABSENT afterward.
K3 write_file WITH approval -> executes, exact content, and the trail
   records approval_requested -> approval_granted -> tool_execute in
   that order (record-before-execute proven by trail sequence).
K4 trail-write fault injection -> action does NOT execute.
"""
import shutil
import time

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
    # Record-before-execute, proven by trail ORDER (sequence), not by
    # comparing the trail clock against the file's mtime: at
    # same-process sequential-write timescales that cross-clock
    # nanosecond comparison is racy under load and cannot reliably
    # order the two writes anyway, while the append-only trail's own
    # order is exact.
    events = store.read_trail(task_id=task["id"])

    def _index(event_type):
        for i, e in enumerate(events):
            if e["type"] == event_type and e["tool"] == "write_file":
                return i
        return None

    exec_idx = _index("tool_execute")
    assert exec_idx is not None, "no tool_execute trail record for write_file"
    req_idx = _index("approval_requested")
    grant_idx = _index("approval_granted")
    assert req_idx is not None, \
        "no approval_requested trail record for write_file"
    assert grant_idx is not None, \
        "no approval_granted trail record for write_file"
    assert req_idx < grant_idx < exec_idx, (
        "trail order is not approval_requested -> approval_granted -> "
        "tool_execute; the execution was not recorded before it ran")
    # Loose sanity only: the execute record's timestamp falls inside
    # the task's own run window (after its first trail event, before
    # now) — a single-clock check; no filesystem times are compared.
    exec_ts_ns = events[exec_idx]["ts_ns"]
    assert events[0]["ts_ns"] <= exec_ts_ns <= time.time_ns()


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
