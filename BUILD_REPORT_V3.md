# Ghost Agent v3 — Every-Page Dashboard Improvements Build Report

Built Nov 1, 2026 (v0.3.0), at Ryan's direction ("improve them all"
after viewing all 9 v2 pages). Local build + verification only: no
GitHub push, no publish, no release, $0 spend. Python stdlib only,
zero dependencies, no build step. All v1+v2 behavior and APIs
preserved; record-before-execute and governance semantics unchanged.

## Sanity gate v3 (known answers) — run FIRST, reported separately

Command: `PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v3.py -v`

**Pass rate: 5/5 (100%)**

| Case | Known answer | Result |
|---|---|---|
| K1 Approvals history | seeded granted + denied + pending; GET /api/approvals returns all 3 with correct statuses + task titles, newest first | PASS |
| K2 Propose is candidate-only | POST /api/memory/propose → appears in candidates, NOT in trusted; empty/whitespace/missing text → 400, nothing created | PASS |
| K3 Activity integrity | sum(activity_by_day) == trail_total in /api/analytics; per-day counts equal the seeded events' own dates (incl. one raw backdated event); 14 consecutive UTC days ending today; /api/overview carries the same series | PASS |
| K4 System health + hygiene | /api/system health.workspace_writable is true on a temp home; with token + API key both set in env, neither value appears anywhere in the serialized payload (endpoint still 401s without the token) | PASS |
| K5 Back-compat under v3 | the v1 gate (4/4) and v2 gate (5/5) still pass in the same run (subprocess: "9 passed") | PASS |

Gate history, honestly: the gate was written before the v3 code and
run once against the v2 codebase to prove it can fail — that run was
**1/5** (only K5 passed, as it must: it pins prior behavior; K1–K4
failed with 404/KeyError because v2 has no /api/approvals, no
/api/memory/propose, no activity_by_day, and no health block). Those
results were discarded. After the v3 implementation, the gate was
re-run from scratch: **5/5**. All three gates together: 14 passed.

## Full test suite

Command: `PYTHONPATH=src python3 -m pytest tests/ -q`

**56 passed, 0 failed** = 42 pre-existing tests (27 v1 + 5 v2 gate +
10 v2 server, all unchanged and passing) + 5 v3 gate tests + 9 new
v3 tests (`tests/test_server_v3.py`: approvals payload/endpoint
empty cases, propose default source + candidate-only over HTTP,
activity zero-fill on an empty store, activity counts landing on
today, system health block fields, health probe leaves no files
behind, system endpoint health, and the v3 page-landmark sweep —
Recent decisions section, memory propose form, logs pause button +
severity filter + download, analytics donut container, system
health section + auth posture + serve commands, board/task/skill
filters, sidebar count badges, confirm-on-deny).

One test-authoring fix during the run (not a product bug): the
landmark test first asserted `id="cmd-local"` with double quotes,
but the page's JS builds that element with single quotes
(`id='cmd-local'`), so the assertion false-fired. The test now checks
for the element name itself; the serve-command blocks were verified
present in the served page either way.

Extra check beyond the suite: the served page's `<script>` block was
extracted and passed `node --check` — JS syntax OK.

## Demo regression (CLI demo, isolated home)

`GHOST_AGENT_HOME=/tmp/ga-v3-demo PYTHONPATH=src python3 -m ghost_agent demo`
exited **0**, identical shape to v1/v2: paused at waiting_approval
for write_file, approved, all 3 steps done, `demo_output.txt`
content exact ("Ghost Agent demo run\n"), candidate skill proposed
(untrusted), 11 trail events. No regression.

## Live HTTP smoke (real serve, seeded with the demo run)

All **200**: `/`, `/api/overview`, `/api/analytics`,
`/api/approvals`, `/api/system`. `POST /api/memory/propose` →
**201** (and the memory landed as a candidate); empty text →
**400**. Spot payloads: /api/approvals returned the demo's granted
approval enriched with its task title; /api/system reported version
0.3.0 with a full health block (python 3.12.3, workspace_writable
true, trail total 12 = 11 demo events + the smoke propose);
analytics activity series had 14 days summing exactly to
trail_total 12. Page landmarks (Recent decisions, mem-form,
log-pause, Auth posture) present in the served HTML.

## What changed

- `src/ghost_agent/server.py` — v3 page + payloads + endpoints:
  - Global: a description under every page title; proper empty
    states on every list/section; inline action feedback lines
    (approve/deny/reject/promote/propose/create) driven by the API
    response; `confirm()` before task deny and memory reject;
    sidebar badges for running tasks and memory candidates next to
    the existing approvals badge.
  - Overview: stat cards link to their pages; 14-day activity bar
    chart (`activity_by_day` in /api/overview, computed server-side
    from real trail timestamps); recent tasks get step-progress
    bars; Demo / Read a file / Write a file template chips fill the
    New task input.
  - Board: column counts in headers; cards show model, step
    progress bar, created time; client-side text filter across
    title/goal/tool; per-column empty states.
  - Approvals: NEW `GET /api/approvals` (all approvals enriched
    with task titles, newest first) feeding a Recent decisions
    history; pending cards render args as labeled rows with
    long values truncated behind a show more/less toggle.
  - Tasks: status filter chips (All/Running/Waiting/Done/Failed) +
    text search; detail adds a chronological trail timeline
    (time, type, tool, detail summary) beside the existing search
    box; step rows show result output or error in a muted block.
  - Memory: NEW `POST /api/memory/propose` — candidate ONLY, never
    trusted directly, empty text → 400 — wired to an Add-a-memory
    form (text + optional source); search filters both lists;
    cards show created date and a verified / not-verified badge.
  - Skills: All/Candidate/Trusted filter chips + text search;
    cards show a horizontal step timeline (tool → tool → …),
    permissions as badges, and the source task's title when it
    resolves from the board data.
  - Analytics: tasks-by-status conic-gradient donut with legend
    (hidden gracefully at zero tasks); approval outcome ratio bar
    (granted/denied/pending with percentages); trail-by-type bars
    now show count AND percentage; the same 14-day activity chart
    (`activity_by_day` in /api/analytics).
  - Logs: Pause/Resume button (pauses only the logs fetch; other
    pages keep refreshing); severity filter (errors: task_failed,
    step_failed, approval_denied, model_fallback, governance_deny,
    any *fail* type; approvals: approval_*; success: task_done,
    step_done, memory_approved, skill_promoted; else other);
    "N of M events" count; search matches highlighted in the
    detail text; Download button saves the currently filtered
    events as JSON. Trail fetch limit raised 100 → 200.
  - System: `health` block in /api/system (python_version,
    server_time_utc, store_home_exists, workspace_exists,
    workspace_writable via a temp-file probe that is deleted
    immediately and never touches the trail, tasks_total,
    trail_events_total); page renders the health section, an Auth
    posture checklist (loopback / token set / fail-closed, PASS or
    WARN badges), and the exact local + token-mode serve commands
    as copyable code lines. No destructive actions added.
- `src/ghost_agent/__init__.py`, `pyproject.toml` — version 0.3.0.
- `README.md` — v3 dashboard description, new endpoints, health
  block, tests section.
- `tests/test_sanity_gate_v3.py`, `tests/test_server_v3.py` — new.
- `BUILD_REPORT_V3.md` — this file.

## Notes / downgrades

* No findings downgraded. Analytics still invents nothing: the
  donut, ratio, percentages, and activity chart all derive from
  real task/approval/trail records, and token/cost remains the
  explicit "token/cost not tracked yet" note.
* Logs search highlighting is best-effort (exact-case match on the
  escaped detail text); the guaranteed part of the spec — the
  "N of M events" filter count — always shows.
* The memory propose endpoint's default source is "human" when the
  caller does not supply one; the dashboard form passes its own
  source field through when filled in.
