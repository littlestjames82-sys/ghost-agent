"""Model adapters and router.

MockModel: deterministic, rule-based planner for tests and the demo.
It is NOT intelligent; it pattern-matches a small set of phrasings.

OpenAICompatibleModel: talks to an OpenAI-compatible /chat/completions
endpoint via urllib. The API key is read from an environment variable
at call time (GHOST_AGENT_API_KEY by default; a provider profile
names its own variable — see providers.py) and is never stored or
logged. Base URL: GHOST_AGENT_BASE_URL (default
https://api.openai.com/v1). Model name: GHOST_AGENT_MODEL (default
"gpt-4o-mini"). A keyless profile (api_key_env None — Ollama, LM
Studio) sends no Authorization header at all.

AnthropicModel (v0.10.0): the native Messages API — POST
{base_url}/v1/messages with x-api-key + anthropic-version headers;
the reply's text blocks are concatenated into the same plan/step
shapes the OpenAI adapter produces, and its usage
{input_tokens, output_tokens} normalizes into the same records.

Router: prefers the real provider when a key is configured, otherwise
MockModel. On provider failure it falls back to MockModel and reports
the fallback reason so the agent loop can record it in the trail.

Provider profiles + strategies (v0.10.0): when a Providers config
(providers.py) is attached, the router's FIRST call for a task is
resolved by the configured strategy — "default" (the named default
profile, else the legacy env adapter), "chain" (the chain's
configured profiles in order; first success owns the task, failures
are recorded), "cheapest" (the lowest-priced configured profile
whose model has known pricing, else "default" behavior) — or by an
explicit per-task profile override. Every result names its owner in
"provider" (a profile name, "env", or "mock"). Once a task has an
owner, later calls go to that owner only: a mid-task provider
failure raises, it never switches brains.

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
    # Anthropic's published list prices for the Claude 4.5 family
    # (anthropic.com/pricing, as of Nov 2026): Sonnet 4.5 at
    # $3.00 input / $15.00 output per million, Haiku 4.5 at
    # $1.00 / $5.00. Exact-name prefixes: other Claude models
    # stay unpriced ("unknown") rather than guessed.
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
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
        # The ecosystem registry (ghost_agent.ecosystem.Ecosystem),
        # wired in by the Agent so the 'status of <project>'
        # pattern can name only projects the registry holds.
        # None in bare unit use — the pattern then falls through.
        self.ecosystem = None

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
        # ecosystem (v0.8.0): 'list projects' / any goal mentioning
        # the ecosystem -> the registry listing; 'status of <name>'
        # -> repo_status, but only for a name the registry actually
        # holds (an unregistered name falls through to the generic
        # patterns below rather than planning a doomed step).
        if "list projects" in lower or "ecosystem" in lower:
            return [{"tool": "list_projects", "args": {},
                     "verify": {"type": "ok"}}]
        m = re.search(r"status of\s+([A-Za-z0-9_.\-]+)", text, re.IGNORECASE)
        if m and self.ecosystem is not None:
            entry = self.ecosystem.get(m.group(1))
            if entry is not None:
                return [{"tool": "repo_status",
                         "args": {"project": entry["name"]},
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
    "append_file{path, content}, shell{command}, "
    "list_projects{layer?}, repo_status{project}, "
    "read_project_file{project, path}."
)

TOOL_NAMES = ("read_file,list_dir,echo_note,search_files,"
              "write_file,append_file,shell,"
              "list_projects,repo_status,read_project_file")


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
        + TOOL_NAMES + ", "
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

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key_env: str | None = "GHOST_AGENT_API_KEY"):
        self.base_url = (base_url or os.environ.get("GHOST_AGENT_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("GHOST_AGENT_MODEL") or "gpt-4o-mini"
        # The env var holding this adapter's key (a provider
        # profile names its own; None = keyless local server).
        self.api_key_env = api_key_env
        self.last_usage: dict | None = None
        # Legacy full-plan answers to a next-step request, keyed by
        # goal and served stepwise by history index (see next_step).
        self._plan_cache: dict[str, list[dict]] = {}

    def _headers(self) -> dict:
        """Request headers for one call. The key is read from the
        adapter's env variable at call time and used in-memory
        only; a keyless adapter sends no Authorization header.
        Raises ModelError when a named variable is unset — the
        same failure shape the legacy path has always produced."""
        headers = {"Content-Type": "application/json"}
        if self.api_key_env is not None:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ModelError(f"{self.api_key_env} is not set")
            headers["Authorization"] = "Bearer " + key
        return headers

    def plan(self, goal: str) -> list[dict]:
        self.last_usage = None
        prompt = build_planner_prompt(goal)
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers=self._headers(),
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
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers=self._headers(),
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


class AnthropicModel:
    """The native Anthropic Messages API adapter (v0.10.0).

    POST {base_url}/v1/messages, the key carried in the x-api-key
    header (read from the adapter's env variable at call time,
    in-memory only) and anthropic-version: 2023-06-01. The reply
    content is a list of blocks; the text blocks are concatenated
    and parsed with exactly the plan/step shapes the
    OpenAI-compatible adapter accepts (a JSON array plan, one
    step object, or {"tool": "done"} — including the legacy
    full-plan buffering). usage {input_tokens, output_tokens}
    normalizes into the provider usage record as prompt/
    completion tokens.
    """
    name = "anthropic"
    is_mock = False

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key_env: str | None = "ANTHROPIC_API_KEY"):
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model or "claude-sonnet-4-5"
        self.api_key_env = api_key_env
        self.last_usage: dict | None = None
        # Legacy full-plan answers to a next-step request, keyed by
        # goal and served stepwise by history index (see next_step).
        self._plan_cache: dict[str, list[dict]] = {}

    def _headers(self) -> dict:
        headers = {"content-type": "application/json",
                   "anthropic-version": "2023-06-01"}
        if self.api_key_env is not None:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ModelError(f"{self.api_key_env} is not set")
            headers["x-api-key"] = key
        return headers

    def _post(self, prompt: str) -> dict:
        """One /v1/messages round trip. Raises ModelError on
        transport failure; records last_usage on success."""
        body = json.dumps({
            "model": self.model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/v1/messages", data=body, method="POST",
            headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ModelError(f"provider request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise ModelError("provider returned an unusable response")
        raw = payload.get("usage")
        raw = raw if isinstance(raw, dict) else {}
        self.last_usage = usage_record(
            payload.get("model") or self.model, "provider",
            {"prompt_tokens": raw.get("input_tokens"),
             "completion_tokens": raw.get("output_tokens")},
            adapter=self.name)
        return payload

    @staticmethod
    def _content_text(payload: dict) -> str:
        """The reply's text: its text blocks concatenated. A
        reply with no usable text block — including an empty
        content list — is an empty response: the same ModelError
        the OpenAI adapter raises for a null message content."""
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise ModelError("provider returned an empty response")
        text = "".join(
            b.get("text") or "" for b in blocks
            if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            raise ModelError("provider returned an empty response")
        return text

    def plan(self, goal: str) -> list[dict]:
        self.last_usage = None
        payload = self._post(build_planner_prompt(goal))
        content = self._content_text(payload)
        try:
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
        except ValueError as exc:
            raise ModelError(f"provider returned an unusable plan: {type(exc).__name__}") from exc

    # ---- plan-act-observe (same contract as the OpenAI adapter) --------
    @staticmethod
    def _normalize_step(step) -> dict:
        if not isinstance(step, dict) or "tool" not in step:
            raise ValueError("bad step")
        step = dict(step)
        step.setdefault("args", {})
        step.setdefault("verify", {"type": "ok"})
        return step

    def _serve_cached(self, goal: str, history: list[dict]) -> dict | None:
        """Serve a buffered full-plan answer by history index
        (see OpenAICompatibleModel._serve_cached)."""
        steps = self._plan_cache.get(goal)
        if steps is None:
            return None
        idx = len(history or [])
        if idx >= len(steps):
            del self._plan_cache[goal]
            return None
        return self._normalize_step(steps[idx])

    def next_step(self, goal: str, history: list[dict]) -> dict | None:
        """Ask the provider for the NEXT step given the history.
        Same contract as OpenAICompatibleModel.next_step: one
        step dict, or None on {"tool": "done"}; ModelError on
        failure; a legacy full-plan array is buffered and served
        stepwise with its usage recorded once, when fetched."""
        self.last_usage = None
        if goal in self._plan_cache:
            return self._serve_cached(goal, history)
        payload = self._post(build_next_step_prompt(goal, history))
        content = self._content_text(payload)
        i_obj = content.find("{")
        i_arr = content.find("[")
        try:
            if i_arr != -1 and (i_obj == -1 or i_arr < i_obj):
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
    """Picks a model and falls back to MockModel on provider failure.

    v0.10.0: a Providers config (providers.py) may be attached.
    With one, a task's FIRST next-step call is resolved by the
    configured strategy (default / chain / cheapest) or by an
    explicit per-task profile override; without one — or when the
    config names nothing configured — the router behaves exactly
    as before: the legacy env adapter (self.real, the implicit
    "env" profile) serves, with MockModel fallback on first-call
    failure. Every result names the task's owner in "provider":
    a profile name, "env", or "mock".
    """

    def __init__(self, prefer_real: bool | None = None, providers=None):
        if prefer_real is None:
            prefer_real = bool(os.environ.get("GHOST_AGENT_API_KEY"))
        self.prefer_real = prefer_real
        self.providers = providers
        self.mock = MockModel()
        self.real = OpenAICompatibleModel() if prefer_real else None
        self._profile_adapters: dict[str, object] = {}

    # ---- profile adapters ------------------------------------------------
    def _adapter_for(self, name: str):
        """The (cached) adapter for a configured profile name.
        Raises ModelError for an unknown profile — callers that
        accept user input validate first (Agent.create_task)."""
        adapter = self._profile_adapters.get(name)
        if adapter is not None:
            return adapter
        profile = self.providers.get(name) if self.providers else None
        if profile is None:
            raise ModelError(f"unknown provider profile: {name}")
        if profile.get("kind") == "anthropic":
            adapter = AnthropicModel(base_url=profile["base_url"],
                                     model=profile["model"],
                                     api_key_env=profile["api_key_env"])
        else:
            adapter = OpenAICompatibleModel(
                base_url=profile["base_url"], model=profile["model"],
                api_key_env=profile["api_key_env"])
        self._profile_adapters[name] = adapter
        return adapter

    # ---- result shaping ----------------------------------------------------
    @staticmethod
    def _with_provider(usage, provider: str):
        """A copy of an adapter's usage record carrying the
        owning provider's name (the trail's model_usage detail
        and the task record both keep it)."""
        if usage is None:
            return None
        out = dict(usage)
        out["provider"] = provider
        return out

    def _adapter_result(self, adapter, step: dict | None,
                        provider: str) -> dict:
        return {"step": step, "done": step is None,
                "model": adapter.name,
                "usage": self._with_provider(
                    getattr(adapter, "last_usage", None), provider),
                "fallback_reason": None, "provider": provider}

    def _real_result(self, step: dict | None) -> dict:
        return {"step": step, "done": step is None,
                "model": self.real.name,
                "usage": self._with_provider(
                    getattr(self.real, "last_usage", None), "env"),
                "fallback_reason": None, "provider": "env"}

    def _mock_result(self, step: dict | None,
                     fallback_reason: str | None = None) -> dict:
        return {"step": step, "done": step is None,
                "model": self.mock.name,
                "usage": self._with_provider(self.mock.last_usage, "mock"),
                "fallback_reason": fallback_reason, "provider": "mock"}

    # ---- legacy full-plan interface ---------------------------------------
    def plan(self, goal: str, task_type: str | None = None) -> dict:
        """Returns {steps, model, fallback_reason, usage, provider}.

        The pre-loop planning interface, kept for compatibility:
        it speaks to the legacy env adapter (self.real) exactly as
        it always has — strategies govern the agent loop's
        next-step calls, not this legacy path."""
        if self.real is not None:
            try:
                steps = self.real.plan(goal)
                usage = getattr(self.real, "last_usage", None) or usage_record(
                    getattr(self.real, "model", None) or self.real.name,
                    "provider", None, adapter=self.real.name)
                return {"steps": steps, "model": self.real.name,
                        "fallback_reason": None,
                        "usage": self._with_provider(usage, "env"),
                        "provider": "env"}
            except ModelError as exc:
                return {"steps": self.mock.plan(goal), "model": self.mock.name,
                        "fallback_reason": str(exc),
                        "usage": self._with_provider(self.mock.last_usage,
                                                     "mock"),
                        "provider": "mock"}
        return {"steps": self.mock.plan(goal), "model": self.mock.name,
                "fallback_reason": None,
                "usage": self._with_provider(self.mock.last_usage, "mock"),
                "provider": "mock"}

    # ---- plan-act-observe (v0.7.0; profiles + strategies v0.10.0) ---------
    @staticmethod
    def _adapter_next_step(adapter, goal: str,
                           history: list[dict]) -> dict | None:
        """One next-step call against one adapter. Adapters that
        only implement the legacy full-plan interface are served
        stepwise from their plan by history index (their own
        ModelError still propagates verbatim)."""
        nxt = getattr(adapter, "next_step", None)
        if nxt is not None:
            return nxt(goal, history)
        steps = adapter.plan(goal)
        idx = len(history or [])
        if idx >= len(steps):
            return None
        step = steps[idx]
        return dict(step, args=dict(step.get("args") or {}))

    def _real_next_step(self, goal: str, history: list[dict]) -> dict | None:
        return self._adapter_next_step(self.real, goal, history)

    def _call_profile(self, name: str, goal: str,
                      history: list[dict]) -> dict:
        """One first call against a named profile. ModelError
        propagates — the strategy layer decides what a failure
        means; mid-task calls let it fail the task."""
        adapter = self._adapter_for(name)
        step = self._adapter_next_step(adapter, goal, history)
        return self._adapter_result(adapter, step, name)

    def _try_profile(self, name: str, goal: str, history: list[dict],
                     failures: list[str]) -> dict | None:
        """One strategy attempt against a named profile: the
        result on success, or None with the failure appended to
        `failures` (as "name: reason") for the record."""
        try:
            return self._call_profile(name, goal, history)
        except ModelError as exc:
            failures.append(f"{name}: {exc}")
            return None

    def _mock_fallback(self, goal: str, history: list[dict],
                       reason: str) -> dict:
        return self._mock_result(self.mock.next_step(goal, history),
                                 fallback_reason=reason)

    def _first_call(self, goal: str, history: list[dict],
                    requested: str | None) -> dict:
        """Resolve a task's FIRST next-step call: an explicit
        profile override when given, else the configured
        strategy, else the legacy env behavior. First-call
        provider failures fall back to MockModel with the reason
        recorded — the v0.7.0 honesty rule, unchanged."""
        if requested is not None:
            # Agent.create_task validated the name; a failure here
            # is a provider failure like any other first call.
            failures: list[str] = []
            result = self._try_profile(requested, goal, history, failures)
            if result is not None:
                return result
            return self._mock_fallback(goal, history, failures[0])
        providers = self.providers
        strategy = providers.strategy if providers is not None else "default"
        if providers is not None and strategy == "chain":
            failures: list[str] = []
            for name in providers.chain:
                profile = providers.get(name)
                if profile is None or not providers.configured(profile):
                    continue  # unconfigured chain entries are skipped
                result = self._try_profile(name, goal, history, failures)
                if result is not None:
                    if failures:
                        # The chain hopped: record what it hopped
                        # over, even though a real provider serves.
                        result["fallback_reason"] = \
                            "chain fallback: " + "; ".join(failures)
                    return result
            if failures:
                return self._mock_fallback(
                    goal, history,
                    "chain failed: " + "; ".join(failures))
            return self._mock_fallback(
                goal, history,
                "chain strategy: no configured profiles to try")
        if providers is not None and strategy == "cheapest":
            pick = self._cheapest_pick()
            if pick is not None:
                failures = []
                result = self._try_profile(pick, goal, history, failures)
                if result is not None:
                    return result
                return self._mock_fallback(goal, history, failures[0])
            # No configured profile is priced: behave as "default".
        if providers is not None and providers.default:
            profile = providers.get(providers.default)
            if profile is not None and providers.configured(profile):
                failures = []
                result = self._try_profile(providers.default, goal,
                                           history, failures)
                if result is not None:
                    return result
                return self._mock_fallback(goal, history, failures[0])
        # Legacy env behavior (exactly v0.9.0): self.real serves,
        # MockModel when there is no real adapter.
        if self.real is not None:
            try:
                return self._real_result(
                    self._real_next_step(goal, history))
            except ModelError as exc:
                return self._mock_fallback(goal, history, str(exc))
        return self._mock_result(self.mock.next_step(goal, history))

    def _cheapest_pick(self) -> str | None:
        """The cheapest strategy's pick: among CONFIGURED
        profiles whose model has known pricing, the lowest
        (input + output) per-million sum — estimate_cost_usd at
        1M/1M is exactly that sum. None when no configured
        profile is priced."""
        providers = self.providers
        if providers is None:
            return None
        best: tuple[float, str] | None = None
        for name, profile in providers.profiles.items():
            if not providers.configured(profile):
                continue
            total = estimate_cost_usd(profile["model"], 1_000_000, 1_000_000)
            if total is None:
                continue
            if best is None or total < best[0]:
                best = (total, name)
        return best[1] if best is not None else None

    def next_step(self, goal: str, history: list[dict],
                  brain: str | None = None,
                  provider: str | None = None) -> dict:
        """One plan-act-observe call. Returns {step, done, model,
        usage, fallback_reason, provider}; step is None when the
        model is done.

        Fallback honesty: with brain=None (a task's FIRST call) a
        provider failure falls back to MockModel for the whole
        task, with the reason reported. Once a task has a brain
        (its recorded model name) and an owner (its recorded
        provider), calls go to that owner only — a provider
        failure raises ModelError so the agent can fail the task
        honestly instead of silently switching brains mid-task.
        """
        if brain is None:
            return self._first_call(goal, history, provider)
        if provider is not None and provider not in ("env", "mock"):
            # The task is owned by a named profile.
            if brain == self.mock.name:
                return self._mock_result(self.mock.next_step(goal, history))
            adapter = self._adapter_for(provider)
            if brain != adapter.name:
                raise ModelError(f"unknown brain for task: {brain}")
            return self._adapter_result(
                adapter,
                self._adapter_next_step(adapter, goal, history),
                provider)
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
