"""Skills: candidate skills proposed from successful runs.

A candidate skill is UNTRUSTED until a human promotes it. It records
the run's steps, the tools used, and the permissions those tools need.
"""
from __future__ import annotations

import re

from .store import Store, new_id
from .tools import tool_kind


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "skill"


class Skills:
    def __init__(self, store: Store):
        self.store = store

    def propose_from_run(self, task: dict) -> dict:
        steps = []
        tools_used: list[str] = []
        permissions: list[str] = []
        for step in task.get("steps", []):
            tool = step.get("tool")
            steps.append({"tool": tool, "args": step.get("args", {})})
            if tool and tool not in tools_used:
                tools_used.append(tool)
                kind = tool_kind(tool)
                if kind and kind not in permissions:
                    permissions.append(kind)
        skill = {
            "id": new_id("skl"),
            "name": _slug(task.get("title") or task.get("goal") or "run"),
            "status": "candidate",  # candidate | trusted
            "trusted": False,
            "from_task": task.get("id"),
            "steps": steps,
            "tools": tools_used,
            "permissions": permissions,
        }
        self.store.try_trail("skill_candidate",
                             {"skill_id": skill["id"], "from_task": task.get("id")},
                             task_id=task.get("id"))
        return self.store.put("skills", skill)

    def promote(self, skill_id: str) -> dict | None:
        skill = self.store.get("skills", skill_id)
        if skill is None or skill.get("status") != "candidate":
            return None
        out = self.store.update("skills", skill_id, status="trusted", trusted=True)
        self.store.try_trail("skill_promoted", {"skill_id": skill_id})
        return out

    def list_all(self) -> list[dict]:
        return self.store.list("skills")

    def list_trusted(self) -> list[dict]:
        return [s for s in self.list_all() if s.get("trusted")]
