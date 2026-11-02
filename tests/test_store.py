import json

import pytest

from ghost_agent.store import Store, new_id


def test_put_get_list_update(store):
    e = store.put("tasks", {"id": new_id("tsk"), "title": "x", "status": "pending"})
    assert store.get("tasks", e["id"])["title"] == "x"
    assert [i["id"] for i in store.list("tasks")] == [e["id"]]
    store.update("tasks", e["id"], status="done")
    assert store.get("tasks", e["id"])["status"] == "done"
    assert store.get("tasks", "nope") is None


def test_entities_have_timestamps(store):
    e = store.put("memories", {"id": new_id("mem"), "text": "t"})
    assert e["created_at"] and e["updated_at"]


def test_trail_append_and_read(store):
    ev = store.append_trail("test_event", {"k": 1}, task_id="tsk_1")
    events = store.read_trail()
    assert len(events) == 1 and events[0]["id"] == ev["id"]
    assert store.read_trail(task_id="tsk_1")[0]["detail"] == {"k": 1}
    assert store.read_trail(task_id="other") == []
    raw = store.trail_path.read_text().strip()
    assert json.loads(raw)["type"] == "test_event"


def test_trail_append_raises_when_unwritable(tmp_path):
    s = Store(tmp_path / "h")
    s.trail_path.mkdir()  # directory where the file belongs
    with pytest.raises(OSError):
        s.append_trail("boom", {})


def test_home_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_AGENT_HOME", str(tmp_path / "envhome"))
    s = Store()
    assert s.home == tmp_path / "envhome"
    assert s.workspace.is_dir()
