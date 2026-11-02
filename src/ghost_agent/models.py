"""Model adapters and router.

MockModel: deterministic, rule-based planner for tests and the demo.
It is NOT intelligent; it pattern-matches a small set of phrasings.

OpenAICompatibleModel: talks to an OpenAI-compatible /chat/completions
endpoint via urllib. The API key is read from the GHOST_AGENT_API_KEY
environment variable at call time and is never stored or logged.
Base URL: GHOST_AGENT_BASE_URL (default https://api.openai.com/v1).
Model name: GHOST_AGENT_MODEL (default "gpt-4o-mini").

Router: prefers the real provider when a key is configured, otherwise
MockModel. On provider failure it falls back to MockModel and reports
the fallback reason so the agent loop can record it in the trail.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


class ModelError(Exception):
    pass


DEMO_STEPS = [
    {"tool": "echo_note", "args": {"text": "Ghost Agent demo run starting"},
     "verify": {"type": "ok"}},
    {"tool": "write_file",
     "args": {"path": "demo_output.txt", "content": "Ghost Agent demo run\n"},
     "verify": {"type": "file_content", "path": "demo_output.txt",
                "content": "Ghost Agent demo run\n"}},
    {"tool": "read_file", "args": {"path": "demo_output.txt"},
     "verify": {"type": "ok"}},
]


class MockModel:
    name = "mock"
    is_mock = True

    def plan(self, goal: str) -> list[dict]:
        text = (goal or "").strip()
        lower = text.lower()
        if lower == "demo" or lower.startswith("demo:") or lower.startswith("demo "):
            return [dict(s, args=dict(s["args"])) for s in DEMO_STEPS]
        steps: list[dict] = []
        m = re.search(r"write\s+(?:file\s+)?([A-Za-z0-9_./-]+)\s*(?:(?:with|containing)\s*:?|:)\s*(.+)",
                      text, re.IGNORECASE | re.DOTALL)
        written = None
        if m:
            written = m.group(1)
            content = m.group(2).strip()
            steps.append({"tool": "write_file",
                          "args": {"path": written, "content": content},
                          "verify": {"type": "file_content", "path": written,
                                     "content": content}})
        m = re.search(r"read\s+(?:file\s+)?([A-Za-z0-9_./-]+)", text, re.IGNORECASE)
        if m and m.group(1) != written:
            steps.append({"tool": "read_file", "args": {"path": m.group(1)},
                          "verify": {"type": "ok"}})
        elif m and written:
            steps.append({"tool": "read_file", "args": {"path": m.group(1)},
                          "verify": {"type": "ok"}})
        if "list" in lower and not steps:
            steps.append({"tool": "list_dir", "args": {"path": "."},
                          "verify": {"type": "ok"}})
        m = re.search(r"run\s+command\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            steps.append({"tool": "shell", "args": {"command": m.group(1).strip()},
                          "verify": {"type": "exit_zero"}})
        if not steps:
            steps.append({"tool": "echo_note", "args": {"text": text},
                          "verify": {"type": "ok"}})
        return steps


class OpenAICompatibleModel:
    name = "openai-compatible"
    is_mock = False

    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.environ.get("GHOST_AGENT_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("GHOST_AGENT_MODEL") or "gpt-4o-mini"

    def plan(self, goal: str) -> list[dict]:
        key = os.environ.get("GHOST_AGENT_API_KEY")
        if not key:
            raise ModelError("GHOST_AGENT_API_KEY is not set")
        prompt = (
            "You are a planner. Reply with ONLY a JSON array of steps. "
            "Each step: {\"tool\": one of read_file,list_dir,echo_note,write_file,shell, "
            "\"args\": {...}, \"verify\": {\"type\": \"ok\"}}. Goal: " + goal
        )
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key},  # key used in-memory only
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelError(f"provider request failed: {type(exc).__name__}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
            start, end = content.index("["), content.rindex("]") + 1
            steps = json.loads(content[start:end])
            if not isinstance(steps, list) or not steps:
                raise ValueError("empty plan")
            for s in steps:
                if not isinstance(s, dict) or "tool" not in s:
                    raise ValueError("bad step")
                s.setdefault("args", {})
                s.setdefault("verify", {"type": "ok"})
            return steps
        except (KeyError, IndexError, ValueError) as exc:
            raise ModelError(f"provider returned an unusable plan: {type(exc).__name__}") from exc


class Router:
    """Picks a model and falls back to MockModel on provider failure."""

    def __init__(self, prefer_real: bool | None = None):
        if prefer_real is None:
            prefer_real = bool(os.environ.get("GHOST_AGENT_API_KEY"))
        self.prefer_real = prefer_real
        self.mock = MockModel()
        self.real = OpenAICompatibleModel() if prefer_real else None

    def plan(self, goal: str, task_type: str | None = None) -> dict:
        """Returns {steps, model, fallback_reason}."""
        if self.real is not None:
            try:
                steps = self.real.plan(goal)
                return {"steps": steps, "model": self.real.name, "fallback_reason": None}
            except ModelError as exc:
                return {"steps": self.mock.plan(goal), "model": self.mock.name,
                        "fallback_reason": str(exc)}
        return {"steps": self.mock.plan(goal), "model": self.mock.name,
                "fallback_reason": None}
