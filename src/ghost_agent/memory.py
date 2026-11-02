"""Trusted memory with a candidate -> approve/reject flow.

Nothing enters trusted memory without approval. Every memory carries
provenance: source, a verified flag, and superseded_by when replaced.
"""
from __future__ import annotations

from .store import Store, new_id, now_iso


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def propose(self, text: str, source: str = "agent") -> dict:
        mem = {
            "id": new_id("mem"),
            "text": text,
            "status": "candidate",  # candidate | trusted | rejected | superseded
            "source": source,
            "verified": False,
            "superseded_by": None,
        }
        self.store.try_trail("memory_candidate", {"memory_id": mem["id"], "source": source})
        return self.store.put("memories", mem)

    def approve(self, memory_id: str) -> dict | None:
        mem = self.store.get("memories", memory_id)
        if mem is None or mem.get("status") != "candidate":
            return None
        out = self.store.update("memories", memory_id, status="trusted", verified=True)
        self.store.try_trail("memory_approved", {"memory_id": memory_id})
        return out

    def reject(self, memory_id: str) -> dict | None:
        mem = self.store.get("memories", memory_id)
        if mem is None or mem.get("status") != "candidate":
            return None
        out = self.store.update("memories", memory_id, status="rejected")
        self.store.try_trail("memory_rejected", {"memory_id": memory_id})
        return out

    def list_trusted(self) -> list[dict]:
        return [m for m in self.store.list("memories") if m.get("status") == "trusted"]

    def list_candidates(self) -> list[dict]:
        return [m for m in self.store.list("memories") if m.get("status") == "candidate"]

    def list_all(self) -> list[dict]:
        return self.store.list("memories")

    def search(self, query: str) -> list[dict]:
        q = query.lower()
        return [m for m in self.list_trusted() if q in m.get("text", "").lower()]

    def supersede(self, old_id: str, new_text: str, source: str = "human") -> dict | None:
        """Replace a trusted memory. The replacement enters as trusted
        only because the caller is explicitly superseding (a human act);
        the old record is kept and marked superseded_by the new id."""
        old = self.store.get("memories", old_id)
        if old is None or old.get("status") != "trusted":
            return None
        new = {
            "id": new_id("mem"),
            "text": new_text,
            "status": "trusted",
            "source": source,
            "verified": True,
            "superseded_by": None,
            "supersedes": old_id,
        }
        new = self.store.put("memories", new)
        self.store.update("memories", old_id, status="superseded",
                          superseded_by=new["id"])
        self.store.try_trail("memory_superseded",
                             {"old_id": old_id, "new_id": new["id"]})
        return new
