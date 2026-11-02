"""Governance: classify each tool call. Deny-by-default.

read tools  -> allow
write/exec  -> approval (a human must approve before execution)
unknown     -> deny
"""
from __future__ import annotations

from dataclasses import dataclass

from .tools import tool_kind


@dataclass
class Decision:
    action: str  # "allow" | "approval" | "deny"
    reason: str
    kind: str | None = None


def classify(tool_name: str) -> Decision:
    kind = tool_kind(tool_name)
    if kind is None:
        return Decision("deny", f"unknown tool {tool_name!r}: deny-by-default", None)
    if kind == "read":
        return Decision("allow", f"{tool_name} is a read tool: auto-allow", kind)
    return Decision("approval", f"{tool_name} is a {kind} tool: human approval required", kind)
