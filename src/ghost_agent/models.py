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

Usage accounting (v0.6.0): every plan() call records a usage record
on the model (``last_usage``) and the Router returns it. Provider
records carry the response's real ``usage`` numbers; MockModel
records zeros with source "mock". Cost is ESTIMATED only for model
names in MODEL_PRICING_USD_PER_MILLION (longest prefix wins); any
other provider model reports cost None with status "unknown" —
costs are never guessed.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


class ModelError(Exception):
    pass


# Built-in ESTIMATES, USD per 1M tokens (input, output). Only models
# listed here get a cost estimate; everything else is "unknown".
MODEL_PRICING_USD_PER_MILLION = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Meta's published Muse Spark rates (connector doc, Oct 26,
    # 2026): $1.25 input / $4.25 output per million. The prefix
    # covers muse-spark-1.3 and its contributor variants.
    "muse-spark": (1.25, 4.25),
}


def estimate_cost_usd(model: str | None, prompt_tokens: int,
                      completion_tokens: int) -> float | None:
    """Estimate USD for a call, or None when the model is not in the
    built-in price table (unknown — never guessed)."""
    name = (model or "").strip().lower()
    best = None
    for prefix, rates in MODEL_PRICING_USD_PER_MILLION.items():
        if name.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rates)
    if best is None:
        return None
    in_rate, out_rate = best[1]
    return (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000


def _clean_count(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def usage_record(model: str | None, source: str, raw_usage,
                 adapter: str | None = None) -> dict:
    """Normalize one model call's usage into the accounting record
    stored on tasks and written to the trail as `model_usage`."""
    raw = raw_usage if isinstance(raw_usage, dict) else {}
    prompt = _clean_count(raw.get("prompt_tokens"))
    completion = _clean_count(raw.get("completion_tokens"))
    total = (_clean_count(raw.get("total_tokens"))
             if raw.get("total_tokens") is not None else prompt + completion)
    if source == "provider":
        cost = estimate_cost_usd(model, prompt, completion)
        status = "estimated" if cost is not None else "unknown"
    elif source == "mock":
        cost, status = 0.0, "not_applicable"
    else:
        cost, status = None, "unknown"
    return {
        "model": model or "unknown",
        "adapter": adapter,
        "source": source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated_cost_usd": cost,
        "cost_status": status,
        "usage_reported": isinstance(raw_usage, dict),
    }


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

    def __init__(self):
        self.last_usage = usage_record(
            "mock", "mock",
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    def plan(self, goal: str) -> list[dict]:
        self.last_usage = usage_record(
            "mock", "mock",
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        text = (goal or "").strip()
        lower = text.lower()
        if lower == "demo" or lower.startswith("demo:") or lower.startswith("demo "):
            return [dict(s, args=dict(s["args"])) for s in DEMO_STEPS]
        # summarize <file>: read it, then SAY what a real model would
        # do. MockModel does not compose summaries — no fake intelligence.
        m = re.search(r"summarize\s+(?:file\s+)?([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)",
                      text, re.IGNORECASE)
        if m:
            path = m.group(1)
            return [
                {"tool": "read_file", "args": {"path": path},
                 "verify": {"type": "ok"}},
                {"tool": "echo_note",
                 "args": {"text": f"MockModel plan: read {path}; a real model "
                                  "would compose the summary from its content"},
                 "verify": {"type": "ok"}},
            ]
        # find <text> in <path> / search <path> for <text> -> search_files
        m = re.search(r"find\s+(.+?)\s+in\s+([A-Za-z0-9_./-]+)\s*$",
                      text, re.IGNORECASE | re.DOTALL)
        if m:
            return [{"tool": "search_files",
                     "args": {"query": m.group(1).strip(), "path": m.group(2)},
                     "verify": {"type": "ok"}}]
        m = re.search(r"search\s+([A-Za-z0-9_./-]+)\s+for\s+(.+?)\s*$",
                      text, re.IGNORECASE | re.DOTALL)
        if m:
            return [{"tool": "search_files",
                     "args": {"query": m.group(2).strip(), "path": m.group(1)},
                     "verify": {"type": "ok"}}]
        steps: list[dict] = []
        # append <text> to <file>: a WRITE-class step (append_file),
        # so it pauses for approval exactly like write_file.
        m = re.search(r"append\s+(.+?)\s+to\s+([A-Za-z0-9_./-]+)\s*$",
                      text, re.IGNORECASE | re.DOTALL)
        appended = None
        if m:
            appended = m.group(2)
            steps.append({"tool": "append_file",
                          "args": {"path": appended, "content": m.group(1)},
                          "verify": {"type": "ok"}})
        m = None if appended else re.search(
            r"write\s+(?:file\s+)?([A-Za-z0-9_./-]+)\s*(?:(?:with|containing)\s*:?|:)\s*(.+)",
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

    def next_step(self, goal: str, history: list[dict]) -> dict | None:
        """Plan-act-observe interface (v0.7.0): serve the existing
        deterministic full plan one step at a time, indexed by how
        many steps history says already ran. Returns None when the
        plan is exhausted (the loop's 'done'). Same steps, same
        order, same outcomes as plan() — just drip-fed."""
        steps = self.plan(goal)
        idx = len(history or [])
        if idx >= len(steps):
            return None
        step = steps[idx]
        return dict(step, args=dict(step.get("args") or {}))


PLANNER_GUIDANCE = (
    "Planning rules: "
    "(1) When the goal asks you to write, create, or summarize content, "
    "prefer write_file with content you compose yourself from the sources "
    "you read; do not use shell to extract, assemble, or generate composed "
    "content (no grep/sed/awk/head pipelines as a writing shortcut). "
    "(2) Use shell only when the goal explicitly asks to run a command, "
    "or for purely mechanical operations that do not create composed "
    "content. "
    "(3) Order steps so every read of a source file comes before any "
    "write that depends on its content."
)


# Exact argument signatures, taught verbatim in every planner
# prompt. The live 10-goal gauntlet on v0.6.0 (Nov 2, 2026) lost
# 3 of 10 goals to one bug: the model proposed search_files with a
# wrong argument name and the task died on "missing args for
# search_files: ['query']" — the prompt had never named the args.
TOOL_SIGNATURES = (
    "Tools and their exact args (use exactly these names): "
    "read_file{path}, list_dir{path}, echo_note{text}, "
    "search_files{query, path?}, write_file{path, content}, "
    "append_file{path, content}, shell{command}."
)

TOOL_NAMES = ("read_file,list_dir,echo_note,search_files,"
              "write_file,append_file,shell")


def build_planner_prompt(goal: str) -> str:
    """The planner prompt for the OpenAI-compatible adapter.

    The guidance exists because of the first live-model run (Muse
    Spark 1.3, Nov 1, 2026): the model planned a shell grep/sed
    one-liner to mechanically extract a 'summary' instead of reading
    the source and composing the file with write_file. The run was
    governed correctly; the prompt invited the shortcut.
    """
    return (
        "You are a planner. Reply with ONLY a JSON array of steps. "
        "Each step: {\"tool\": one of "
        "read_file,list_dir,echo_note,search_files,write_file,append_file,shell, "
        "\"args\": {...}, \"verify\": {\"type\": \"ok\"}}. "
        + TOOL_SIGNATURES + " "
        + PLANNER_GUIDANCE + " Goal: " + goal
    )


def format_history(history: list[dict]) -> str:
    """Render completed steps (with their observed outputs) for a
    next-step prompt. This is the 'observe' in plan-act-observe:
    the model plans the next step with earlier outputs in view."""
    if not history:
        return "Steps completed so far: none."
    lines = ["Steps completed so far:"]
    for i, h in enumerate(history, start=1):
        try:
            args_txt = json.dumps(h.get("args") or {})
        except (TypeError, ValueError):
            args_txt = str(h.get("args"))
        lines.append(f"{i}. {h.get('tool')} {args_txt} -> {h.get('status')}")
        output = (h.get("output") or "").strip()
        if output:
            lines.append(f"   output: {output}")
    return "\n".join(lines)


def build_next_step_prompt(goal: str, history: list[dict]) -> str:
    """The plan-act-observe prompt (v0.7.0): one step at a time,
    with the history of what already ran and what it produced."""
    return (
        "You are planning a task one step at a time. Reply with "
        "ONLY a JSON object for the NEXT step: {\"tool\": one of "
        + TOOL_NAMES + ", \"args\": {...}, \"verify\": {\"type\": \"ok\"}} "
        "— or {\"tool\": \"done\"} when the goal is fully achieved "
        "and nothing remains to do. "
        + TOOL_SIGNATURES + " "
        + PLANNER_GUIDANCE + " "
        "Work efficiently: do not re-read a file whose output "
        "already appears in the history unless you need a specific "
        "part that was cut off. Keep track of the facts you have "
        "gathered as you go. As soon as the goal's deliverable "
        "exists (for example the requested file has been written), "
        "answer {\"tool\": \"done\"} immediately instead of doing "
        "more verification reads. "
        "Goal: " + goal + "\n"
        + format_history(history)
    )


class OpenAICompatibleModel:
    name = "openai-compatible"
    is_mock = False

    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.environ.get("GHOST_AGENT_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("GHOST_AGENT_MODEL") or "gpt-4o-mini"
        self.last_usage: dict | None = None
        # Legacy full-plan answers to a next-step request, keyed by
        # goal and served stepwise by history index (see next_step).
        self._plan_cache: dict[str, list[dict]] = {}

    def plan(self, goal: str) -> list[dict]:
        self.last_usage = None
        key = os.environ.get("GHOST_AGENT_API_KEY")
        if not key:
            raise ModelError("GHOST_AGENT_API_KEY is not set")
        prompt = build_planner_prompt(goal)
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
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelError(f"provider request failed: {type(exc).__name__}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ModelError("provider returned an empty response")
            start, end = content.index("["), content.rindex("]") + 1
            steps = json.loads(content[start:end])
            if not isinstance(steps, list) or not steps:
                raise ValueError("empty plan")
            for s in steps:
                if not isinstance(s, dict) or "tool" not in s:
                    raise ValueError("bad step")
                s.setdefault("args", {})
                s.setdefault("verify", {"type": "ok"})
            self.last_usage = usage_record(
                payload.get("model") or self.model, "provider",
                payload.get("usage"), adapter=self.name)
            return steps
        except (KeyError, IndexError, ValueError) as exc:
            raise ModelError(f"provider returned an unusable plan: {type(exc).__name__}") from exc

    # ---- plan-act-observe (v0.7.0) ------------------------------------
    def _post(self, prompt: str) -> dict:
        """One chat/completions round trip. Raises ModelError on
        transport failure; records last_usage on success."""
        key = os.environ.get("GHOST_AGENT_API_KEY")
        if not key:
            raise ModelError("GHOST_AGENT_API_KEY is not set")
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
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelError(f"provider request failed: {type(exc).__name__}") from exc
        self.last_usage = usage_record(
            payload.get("model") or self.model, "provider",
            payload.get("usage"), adapter=self.name)
        return payload

    @staticmethod
    def _normalize_step(step) -> dict:
        if not isinstance(step, dict) or "tool" not in step:
            raise ValueError("bad step")
        step = dict(step)
        step.setdefault("args", {})
        step.setdefault("verify", {"type": "ok"})
        return step

    def _serve_cached(self, goal: str, history: list[dict]) -> dict | None:
        """Serve a buffered full-plan answer by history index.
        Returns the step, or None when the buffered plan is
        exhausted (done). Cache hits make NO network call and
        record NO usage — the usage was recorded when the plan
        was fetched."""
        steps = self._plan_cache.get(goal)
        if steps is None:
            return None
        idx = len(history or [])
        if idx >= len(steps):
            del self._plan_cache[goal]
            return None
        return self._normalize_step(steps[idx])

    def next_step(self, goal: str, history: list[dict]) -> dict | None:
        """Ask the provider for the NEXT step given the history of
        completed steps and their outputs. Returns one step dict,
        or None when the provider answers {"tool": "done"} (or a
        buffered plan runs out). Raises ModelError on failure.

        A provider that ignores the one-step instruction and
        answers with a full JSON array (the pre-v0.7.0 plan shape)
        is handled too: the array is buffered under the goal and
        served stepwise by history index, so its usage is recorded
        exactly once, when it was fetched.
        """
        self.last_usage = None
        if goal in self._plan_cache:
            return self._serve_cached(goal, history)
        payload = self._post(build_next_step_prompt(goal, history))
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                f"provider returned an unusable step: {type(exc).__name__}") from exc
        if not isinstance(content, str) or not content.strip():
            # Live-found (v0.7.0 gauntlet, goal 10): Spark can answer
            # with a null message content. That is a provider failure
            # to record honestly, never an AttributeError crash.
            raise ModelError("provider returned an empty response")
        i_obj = content.find("{")
        i_arr = content.find("[")
        try:
            if i_arr != -1 and (i_obj == -1 or i_arr < i_obj):
                # Legacy full-plan shape: buffer and serve stepwise.
                raw = json.loads(content[i_arr:content.rindex("]") + 1])
                if not isinstance(raw, list) or not raw:
                    raise ValueError("empty plan")
                steps = [self._normalize_step(s) for s in raw]
                self._plan_cache[goal] = steps
                return self._serve_cached(goal, history)
            if i_obj == -1:
                raise ValueError("no step object")
            step, _ = json.JSONDecoder().raw_decode(content[i_obj:])
            step = self._normalize_step(step)
        except ValueError as exc:
            raise ModelError(
                f"provider returned an unusable step: {type(exc).__name__}") from exc
        if step.get("tool") == "done":
            return None
        return step


class Router:
    """Picks a model and falls back to MockModel on provider failure."""

    def __init__(self, prefer_real: bool | None = None):
        if prefer_real is None:
            prefer_real = bool(os.environ.get("GHOST_AGENT_API_KEY"))
        self.prefer_real = prefer_real
        self.mock = MockModel()
        self.real = OpenAICompatibleModel() if prefer_real else None

    def plan(self, goal: str, task_type: str | None = None) -> dict:
        """Returns {steps, model, fallback_reason, usage}."""
        if self.real is not None:
            try:
                steps = self.real.plan(goal)
                usage = getattr(self.real, "last_usage", None) or usage_record(
                    getattr(self.real, "model", None) or self.real.name,
                    "provider", None, adapter=self.real.name)
                return {"steps": steps, "model": self.real.name,
                        "fallback_reason": None, "usage": usage}
            except ModelError as exc:
                return {"steps": self.mock.plan(goal), "model": self.mock.name,
                        "fallback_reason": str(exc),
                        "usage": self.mock.last_usage}
        return {"steps": self.mock.plan(goal), "model": self.mock.name,
                "fallback_reason": None, "usage": self.mock.last_usage}

    # ---- plan-act-observe (v0.7.0) ------------------------------------
    def _real_next_step(self, goal: str, history: list[dict]) -> dict | None:
        """One next-step call against the real adapter. Adapters
        that only implement the legacy full-plan interface are
        served stepwise from their plan by history index (their
        own ModelError still propagates verbatim)."""
        nxt = getattr(self.real, "next_step", None)
        if nxt is not None:
            return nxt(goal, history)
        steps = self.real.plan(goal)
        idx = len(history or [])
        if idx >= len(steps):
            return None
        step = steps[idx]
        return dict(step, args=dict(step.get("args") or {}))

    def _real_result(self, step: dict | None) -> dict:
        return {"step": step, "done": step is None,
                "model": self.real.name,
                "usage": getattr(self.real, "last_usage", None),
                "fallback_reason": None}

    def _mock_result(self, step: dict | None,
                     fallback_reason: str | None = None) -> dict:
        return {"step": step, "done": step is None,
                "model": self.mock.name,
                "usage": self.mock.last_usage,
                "fallback_reason": fallback_reason}

    def next_step(self, goal: str, history: list[dict],
                  brain: str | None = None) -> dict:
        """One plan-act-observe call. Returns {step, done, model,
        usage, fallback_reason}; step is None when the model is
        done.

        Fallback honesty: with brain=None (a task's FIRST call) a
        provider failure falls back to MockModel for the whole
        task, exactly as plan() always did, with the reason
        reported. Once a task has a brain (its recorded model
        name), calls go to that brain only — a provider failure
        raises ModelError so the agent can fail the task honestly
        instead of silently switching brains mid-task.
        """
        if brain is None:
            if self.real is not None:
                try:
                    return self._real_result(
                        self._real_next_step(goal, history))
                except ModelError as exc:
                    return self._mock_result(
                        self.mock.next_step(goal, history),
                        fallback_reason=str(exc))
            return self._mock_result(self.mock.next_step(goal, history))
        if brain == self.mock.name:
            return self._mock_result(self.mock.next_step(goal, history))
        # The task's brain is the real provider. If this process
        # built its Router without one (e.g. a CLI approve in an
        # env with no key), construct the adapter now — it reads
        # the key at call time and raises ModelError if unset, so
        # the failure stays honest and attributed.
        if self.real is None:
            self.real = OpenAICompatibleModel()
        if brain != self.real.name:
            raise ModelError(f"unknown brain for task: {brain}")
        return self._real_result(self._real_next_step(goal, history))
