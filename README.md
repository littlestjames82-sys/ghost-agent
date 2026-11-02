![Ghost Agent](assets/ghost-agent-icon.jpg)

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

# Dashboard v4 (sidebar UI: overview, board, approvals, tasks, chat,
# memory, skills, analytics, logs, schedules, system):
PYTHONPATH=src python3 -m ghost_agent serve [--host 127.0.0.1] [--port 8770]

# Non-loopback bind (refused unless the token is set):
GHOST_AGENT_DASHBOARD_TOKEN=... PYTHONPATH=src python3 -m ghost_agent serve --host 0.0.0.0

# CLI:
PYTHONPATH=src python3 -m ghost_agent tasks
PYTHONPATH=src python3 -m ghost_agent approve <task_id>
PYTHONPATH=src python3 -m ghost_agent deny <task_id>
PYTHONPATH=src python3 -m ghost_agent memory
PYTHONPATH=src python3 -m ghost_agent skills
PYTHONPATH=src python3 -m ghost_agent schedules
PYTHONPATH=src python3 -m ghost_agent schedule-add "read notes.txt" --kind interval --interval 45
PYTHONPATH=src python3 -m ghost_agent schedule-add "demo" --kind daily --time 07:15
PYTHONPATH=src python3 -m ghost_agent schedule-remove <schedule_id>
```

State lives under `GHOST_AGENT_HOME` (default `~/.ghost-agent/`):
tasks, memories, skills, and approvals as JSON, the trail as
append-only `trail.jsonl`, and a sandboxed `workspace/` directory that
is the only place tools may touch.

## How it works

* **Store** — JSON/JSONL store. Stable ids, timestamps, and memory
  provenance (source, verified flag, superseded_by).
* **Governance** — every tool call is classified: `read_file`,
  `list_dir`, `echo_note`, `search_files` auto-allow; `write_file`,
  `append_file`, and `shell` require human approval; unknown tools
  are denied by default. Deny marks the step failed and stops the
  run honestly.
* **Tools** — sandboxed to the workspace root: `read_file`, `list_dir`,
  `echo_note`, `search_files` (read — literal substring search with
  file paths + line numbers, capped at 50 matches, binary and >1 MB
  files skipped), `write_file`, `append_file` (write — raw append,
  approval-gated exactly like a write), `shell` (exec, timed out
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
  percentages, the same 14-day activity chart; since v0.6.0 also a
  real Usage block — see "Usage accounting, import, and search
  (v0.6.0)" below),
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
* **Dashboard (v4)** — everything in v3, plus two new pages.
  **Schedules**: recurring tasks — interval (every N minutes) or
  daily (at HH:MM, server local time). The dashboard server ticks
  the scheduler every 20 seconds *while it serves*; **schedules
  only fire while the dashboard server is running** — there is no
  background daemon when it is stopped. A due schedule fires at
  most once per tick (missed intervals are skipped, never
  burst-fired), creates and runs a task through the normal loop,
  and records a `schedule_fired` trail event. Governance is
  untouched: a scheduled write/exec step pauses at
  `waiting_approval` and waits for a human like any other run. The
  page has a create form and per-schedule toggle / run-now /
  delete (with confirm). **Chat**: a conversation-style run view —
  the goal as a user bubble, each step as an agent card with its
  tool, args, status, and output, inline Approve/Deny while a run
  waits, a picker to reopen recent tasks, and live polling while
  the page is open. Also new in v4: a **global search** box in the
  header (tasks, memories, skills, trail), a **command palette**
  (Ctrl/Cmd+K: jump to any page, start a demo task, export state),
  **bulk approvals** (Approve all / Deny all, each decided through
  the same per-item path and individually trailed; only approvals
  pending at call time are decided), **task retry** (failed tasks
  only; creates a new task with the same goal and a "(retry)"
  title, original untouched), a **state export** download, and a
  **density toggle** (Comfortable/Compact, persisted in the
  dashboard origin's localStorage). New APIs:
  `GET /api/schedules`; `POST /api/schedules`
  `{goal, kind, interval_minutes|time_hhmm, title?}` (empty goal,
  interval < 1, or a bad time → 400); `POST /api/schedules/{id}/toggle`;
  `POST /api/schedules/{id}/run`; `DELETE /api/schedules/{id}`
  (unknown id → 404 on all three); `GET /api/search?q=` →
  `{tasks, memories, skills, trail}`, each group capped at 8,
  trail newest first, empty q → all groups empty;
  `POST /api/approvals/decide_all` `{decision: "approved"|"denied"}`
  → `{results, decided, approved, denied}` (bad decision → 400);
  `POST /api/tasks/{id}/retry` (non-failed → 400, unknown → 404);
  `GET /api/export` → a JSON state snapshot
  (`{version, exported_at, tasks, approvals, memories_trusted,
  memories_candidates, skills, schedules, trail}`) served as an
  attachment download. The export holds store data only, so it can
  never contain the dashboard token or a model API key.

## Usage accounting, import, and search (v0.6.0)

* **Real token/cost accounting.** When a provider response includes
  a `usage` block, the numbers are recorded three ways: a
  `model_usage` trail event per model call, cumulative
  `usage_totals` / `usage_by_model` stored on the task, and
  `/api/analytics`' `usage` block (totals + per-model breakdown).
  MockModel calls record usage 0 with source `"mock"`, so the books
  are honest about what is real. Cost is an **estimate only** for
  models in the small built-in price table
  (`MODEL_PRICING_USD_PER_MILLION`, longest prefix wins); any other
  model's cost is reported as **unknown — never a guessed number**.
  If some provider calls are unpriced, the total cost is unknown and
  the priced part is shown separately as `known_estimated_cost_usd`.
* **Safe import.** `POST /api/import` (System page file picker, or
  the API; the token gate applies like everywhere) accepts an
  export snapshot with hard safety semantics: imported memories and
  skills land as **candidates only** (never trusted, provenance
  source prefixed `import:`), imported schedules arrive
  **disabled**, and imported tasks, approvals, and trail are stored
  as **archived history** in separate collections — flagged
  imported, never in the live lists, never re-run. Duplicate ids
  are re-minted rather than overwriting existing records, with
  internal references remapped. A malformed or wrong-shape snapshot
  returns **400 and changes nothing** — the snapshot is validated
  fully before the first write.
* **search_files + planner patterns.** MockModel learned:
  "find <text> in <path>" / "search <path> for <text>" →
  `search_files`; "append <text> to <file>" → `append_file`
  (write-class: it pauses for approval; the file is untouched until
  approved); "summarize <file>" → a read step plus an honest note
  that a real model would compose the summary — MockModel does not
  fake summaries. Skills proposed from runs using the new tools
  record them (and their permission class) correctly.
* **Approvals on the activity chart.** The Overview 14-day chart
  now carries a second, amber series: approvals decided per day
  (`approvals_decided`, counted from the approval_granted /
  approval_denied trail events that were already being recorded).
* **Planner prompt guidance (live-run follow-up).** The first
  live-model run (Muse Spark 1.3 via the Meta API) reached for a
  shell grep/sed one-liner to mechanically "summarize" a file. The
  run was governed correctly, but the prompt invited the shortcut.
  The planner prompt now instructs the model to prefer `write_file`
  with content it composes itself for write/summarize goals, to use
  shell only when the goal explicitly asks for a command (or for
  purely mechanical operations), and to order reads of source files
  before any write that depends on them.

## Models

* **MockModel (default in tests/demo)** — a deterministic, rule-based
  planner that pattern-matches a small set of phrasings
  ("write X with: …", "read X", "run command: …", "demo", and since
  v0.6.0 "find/search …", "append … to …", "summarize …"). It is
  **not intelligent**; it exists so the loop, governance, and trail
  can be exercised and tested without any provider.
* **Real model (optional)** — an OpenAI-compatible adapter (urllib)
  used when you supply your own key at runtime. Its planner prompt
  carries the v0.6.0 guidance described above (compose with
  write_file, shell only when asked, reads before dependent
  writes), and its responses' `usage` blocks feed the accounting:

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
* Schedules fire only while the dashboard server is running (it
  ticks every 20 seconds). Stop the server and schedules wait —
  there is no system daemon, cron entry, or cloud trigger behind
  them.
* No fine-tuned or foundation model in v1 (out of scope by design).

## Tests

```sh
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate.py -v      # v1 known-answer gate first
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v2.py -v   # v2 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v3.py -v   # v3 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v4.py -v   # v4 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v5.py -v   # v5 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v6.py -v   # v6 known-answer gate
PYTHONPATH=src python3 -m pytest tests/ -q                         # full suite
```

See [BUILD_REPORT.md](BUILD_REPORT.md) (v1),
[BUILD_REPORT_V2.md](BUILD_REPORT_V2.md) (v2),
[BUILD_REPORT_V3.md](BUILD_REPORT_V3.md) (v3),
[BUILD_REPORT_V4.md](BUILD_REPORT_V4.md) (v4),
[BUILD_REPORT_V5.md](BUILD_REPORT_V5.md) (v5), and
[BUILD_REPORT_V6.md](BUILD_REPORT_V6.md) (v6) for gate results and
counts.

## Design language (v0.5.0)

The dashboard wears the Trust Ladder design language: a deep-navy
surface (#07030f family) with Ghost violet (#6716f3) accents, content
grouped into rounded "shelves" whose border states meaning at a
glance: solid green = trusted / done / healthy, DASHED amber =
candidate / waiting / untrusted, solid violet = active, muted =
neutral, red = failed. Cards are ranked, carry a text-glyph icon
tile (WRITE, READ, MEM, SKILL - never emoji), and end in a
provenance strip built only from fields that exist (model, step,
status, source, permissions). Navigation is threefold: a grouped
sidebar (Operate / Intelligence / System) on desktop, an All Apps
drawer listing every page once, and a phone bottom bar (Home /
Board / Agents / More) at 820px and under. The brand mark is Ryan's
Ghost Agent icon (`assets/ghost-agent-icon.jpg`, served token-free
so the login screen can show it; a CSS "G" tile takes its place if
the file is ever missing). The reskin changed no API, payload, or
behavior - see BUILD_REPORT_V5.md.
