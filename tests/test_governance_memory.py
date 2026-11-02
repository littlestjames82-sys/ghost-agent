from ghost_agent import governance
from ghost_agent.memory import Memory


def test_governance_classification():
    assert governance.classify("read_file").action == "allow"
    assert governance.classify("list_dir").action == "allow"
    assert governance.classify("echo_note").action == "allow"
    assert governance.classify("write_file").action == "approval"
    assert governance.classify("shell").action == "approval"


def test_governance_unknown_denied():
    d = governance.classify("delete_everything")
    assert d.action == "deny"
    assert "deny-by-default" in d.reason


def test_memory_candidate_flow(store):
    mem = Memory(store)
    cand = mem.propose("the sky unit test is blue", source="test")
    assert cand["status"] == "candidate" and cand["verified"] is False
    assert mem.list_trusted() == []
    approved = mem.approve(cand["id"])
    assert approved["status"] == "trusted" and approved["verified"] is True
    assert [m["id"] for m in mem.list_trusted()] == [cand["id"]]
    assert mem.search("sky")[0]["id"] == cand["id"]
    assert mem.search("absent") == []


def test_memory_reject(store):
    mem = Memory(store)
    cand = mem.propose("bad fact", source="test")
    mem.reject(cand["id"])
    assert mem.list_trusted() == [] and mem.list_candidates() == []
    assert mem.approve(cand["id"]) is None  # rejected cannot be approved


def test_memory_supersede(store):
    mem = Memory(store)
    cand = mem.propose("old fact", source="test")
    mem.approve(cand["id"])
    new = mem.supersede(cand["id"], "new fact")
    assert new["status"] == "trusted"
    old = store.get("memories", cand["id"])
    assert old["status"] == "superseded" and old["superseded_by"] == new["id"]
    assert [m["text"] for m in mem.list_trusted()] == ["new fact"]
    assert mem.supersede("missing", "x") is None
