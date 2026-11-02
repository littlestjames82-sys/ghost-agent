# Ghost Agent

**A Hermes-style persistent, governed operator for Ghost Developer Studio.**

Not a chatbot wrapper. Ghost Agent is a local agent runtime: it takes a
goal, plans steps, and executes tools under governance — read tools run
freely, write/exec tools stop and wait for a human approval, and every
execution is written to an append-only trail *before* it happens. If the
trail record cannot be written, the action does not execute.

```sh
PYTHONPATH=src python3 -m ghost_agent demo
PYTHONPATH=src python3 -m ghost_agent serve   # http://127.0.0.1:8770/
```

**Local first, fail-closed auth (v2, kept in v3).** The dashboard binds
`127.0.0.1` by default; on loopback it needs no token and the warning
banner says so. If you bind it to a non-loopback host, it **refuses to
start** unless `GHOST_AGENT_DASHBOARD_TOKEN` is set — and whenever a
token is set, every `/api/*` call must carry it (`X-Ghost-Token`
header or the cookie set by the page's token form). The token is never
stored or logged. Anyone who can use this dashboard can approve agent
actions on this machine, so keep it on loopback or on a trusted
network behind the token.

By Ghost Developer Studio. MIT licensed. **Zero dependencies**
(Python standard library only), no telemetry, $0 to run.

## How to run

Requires Python 3.10 or newer. Run from a source checkout (no install
needed), or `pip install .` for the `ghost-agent` command.

```sh
# Demo: seeds and runs one full task through the loop with MockModel,
# including one approval step (the demo approves it, as a human would).
PYTHONPATH=src python3 -m ghost_agent demo

# Dashboard v3 (sidebar UI: overview, board, approvals, tasks,
# memory, skills, analytics, logs, system):
PYTHONPATH=src python3 -m ghost_agent serve [--host 127.0.0.1] [--port 8770]

# Non-loopback bind (refused unless the token is set):
GHOST_AGENT_DASHBOARD_TOKEN=... PYTHONPATH=src python3 -m ghost_agent serve --host 0.0.0.0

# CLI:
PYTHONPATH=src python3 -m ghost_agent tasks
PYTHONPATH=src python3 -m ghost_agent approve <task_id>
PYTHONPATH=src python3 -m ghost_agent deny <task_id>
PYTHONPATH=src python3 -m ghost_agent memory
PYTHONPATH=src python3 -m ghost_agent skills
```

State lives under `GHOST_AGENT_HOME` (default `~/.ghost-agent/`):
tasks, memories, skills, and approvals as JSON, the trail as
append-only `trail.jsonl`, and a sandboxed `workspace/` directory that
is the only place tools may touch.

## How it works

* **Store** — JSON/JSONL store. Stable ids, timestamps, and memory
  provenance (source, verified flag, superseded_by).
* **Governance** — every tool call is classified: `read_file`,
  `list_dir`, `echo_note` auto-allow; `write_file` and `shell` require
  human approval; unknown tools are denied by default. Deny marks the
  step failed and stops the run honestly.
* **Tools** — sandboxed to the workspace root: `read_file`, `list_dir`,
  `echo_note` (read), `write_file` (write), `shell` (exec, timed out
  and exit-code captured). Paths escaping the sandbox are refused.
* **Agent loop** — plan, then per step: governance check, approval if
  needed, **record trail**, execute, verify (file exists / exact
  content / exit 0). A fully successful run proposes a candidate skill
  (name, steps, tools, permissions) that stays **untrusted** until a
  human promotes it.
* **Memory** — candidate → approve/reject. Nothing enters trusted
  memory without approval. Trusted memory is searchable, and a trusted
  memory can be superseded (the old record is kept, marked).
* **Dashboard (v3)** — single stdlib-served page (no build step),
  Ghost violet dark theme, left sidebar (collapses to a top bar on
  phones) with live count badges (pending approvals, running tasks,
  memory candidates), one page at a time with the URL hash following
  it, auto-refresh every 5 seconds. Every page has a description and
  an empty state; actions report back inline, and deny/reject asks
  for confirmation first. Pages: **Overview** (stat cards that link
  to their pages, 14-day activity chart from real trail timestamps,
  quick-start task templates, recent tasks with step progress,
  needs-attention), **Board** (kanban Running / Waiting approval /
  Done / Failed with column counts, model + step progress + created
  time on cards, text filter), **Approvals** (pending queue with
  labeled args rows and expandable long values, plus a Recent
  decisions history), **Tasks** (status filter chips + search,
  detail with per-step result/error output, a chronological trail
  timeline, and the trail search box), **Memory** (an Add-a-memory
  form that proposes a *candidate only*, search, candidates with
  approve/reject, trusted with source + verified badge + dates),
  **Skills** (filter chips + search, step timeline per skill,
  permissions as badges, source task title, promote button),
  **Analytics** (honest counts from tasks + trail: status donut,
  approval outcome ratio, by model, steps, events by type with
  percentages, the same 14-day activity chart; token/cost is
  explicitly "not tracked yet" because tasks do not record it),
  **Logs** (live trail tail, type + severity filters, match count,
  search highlighting, pause/resume, download filtered events as
  JSON), **System** (version, home, workspace, model adapter,
  API-key *presence* only, auth mode, a health block, an auth
  posture checklist, and copyable serve commands).
  APIs (v1/v2 sets unchanged, plus): `GET /api/approvals` (all
  approvals enriched with task titles, newest first),
  `POST /api/memory/propose` `{text, source?}` (creates a candidate
  only; empty text → 400), `GET /api/overview` and
  `GET /api/analytics` now include `activity_by_day`, and
  `GET /api/system` includes a `health` block (python version,
  store/workspace existence + writability probe, task and trail
  totals, server time).

## Models

* **MockModel (default in tests/demo)** — a deterministic, rule-based
  planner that pattern-matches a small set of phrasings
  ("write X with: …", "read X", "run command: …", "demo"). It is
  **not intelligent**; it exists so the loop, governance, and trail
  can be exercised and tested without any provider.
* **Real model (optional)** — an OpenAI-compatible adapter (urllib)
  used when you supply your own key at runtime:

  ```sh
  export GHOST_AGENT_API_KEY=...        # your key; never stored or logged
  export GHOST_AGENT_BASE_URL=https://api.openai.com/v1   # optional
  export GHOST_AGENT_MODEL=gpt-4o-mini                    # optional
  ```

  The key is read from the environment at call time only. If the
  provider fails, the router falls back to MockModel and records the
  fallback reason in the trail.

## Honest limits

* MockModel is deterministic, not intelligent. Real planning quality
  needs a real model, with your own key, via the env vars above.
* Dashboard auth is one shared token, not user accounts. Loopback
  needs no token (v1 posture, banner says so); non-loopback binds
  fail closed without `GHOST_AGENT_DASHBOARD_TOKEN`. Still: never
  expose the dashboard to the public internet.
* The `shell` tool runs real commands inside the workspace after
  approval. Approval is the gate; there is no container sandbox.
* One task runs its steps sequentially in-process; approvals pause a
  run in `waiting_approval` state rather than in a background worker.
* No fine-tuned or foundation model in v1 (out of scope by design).

## Tests

```sh
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate.py -v      # v1 known-answer gate first
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v2.py -v   # v2 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v3.py -v   # v3 known-answer gate
PYTHONPATH=src python3 -m pytest tests/ -q                         # full suite
```

See [BUILD_REPORT.md](BUILD_REPORT.md) (v1),
[BUILD_REPORT_V2.md](BUILD_REPORT_V2.md) (v2), and
[BUILD_REPORT_V3.md](BUILD_REPORT_V3.md) (v3) for gate results and
counts.
