# Ghost Agent v4 — Schedules, Chat, Search, Palette Build Report

Built Nov 1, 2026 (v0.4.0), at Ryan's direction ("continue
improving" after v0.3.0). Local build + verification only: no
GitHub push, no publish, no release, $0 spend. Python stdlib only,
zero dependencies, no build step. All v1–v3 behavior and APIs
preserved; record-before-execute and governance semantics
unchanged — a scheduled or bulk-triggered write/exec still waits
for human approval.

## Sanity gate v4 (known answers) — run FIRST, reported separately

Command: `PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v4.py -v`

**Pass rate: 6/6 (100%)**

| Case | Known answer | Result |
|---|---|---|
| K1 Scheduler fires once | interval schedule: tick before due → 0; at due → exactly 1; same instant again → 0; 2.5h later → 1 with next_run advanced past now in whole intervals (no burst); disabled due schedule → 0 | PASS |
| K2 Scheduler respects governance | due schedule with a write goal fires → task ends `waiting_approval`, target file does NOT exist, one pending approval for that task | PASS |
| K3 Bulk decide | 2 seeded pendings; `decide_all` bad decision → 400 with nothing decided; `approved` → decided=2, both results ok, both files on disk with exact content, exactly 2 `approval_granted` trail events; repeat call decides 0 | PASS |
| K4 Search grouping | unique marker planted in a task title, a memory text, a skill name, and a trail event → `/api/search` returns all four groups containing exactly the planted items; absent marker and missing q → all groups empty | PASS |
| K5 Retry + export hygiene | retry of a failed task → new id, same goal, "(retry)" title, new task waits for approval, original byte-identical; retry of a non-failed task → 400; `/api/export` (401 without token) carries all 9 snapshot keys and neither the env dashboard token nor the env API key anywhere in the body | PASS |
| K6 Back-compat under v4 | v1+v2+v3 gates still pass in the same run (subprocess: "14 passed") | PASS |

Gate history, honestly: the gate was written before the v4 code
and run once against the v0.3.0 codebase — it errored at
collection because `ghost_agent.scheduler` did not exist yet.
That run was discarded. After the v4 implementation, the gate was
re-run from scratch: **6/6**. All four gates together: 20 passed.

## Full test suite

Command: `PYTHONPATH=src python3 -m pytest tests/ -q`

**72 passed, 0 failed** = 56 pre-existing tests (27 v1 + 5 v2 gate
+ 10 v2 server + 5 v3 gate + 9 v3 server, all unchanged and
passing) + 6 v4 gate tests + 10 new v4 tests
(`tests/test_server_v4.py`: schedules CRUD + 7 validation failures
over HTTP, daily next-run math incl. fire-and-advance-a-day,
create_schedule unit validation, search group cap of 8 +
case-insensitivity + empty-q payload, search endpoint over HTTP,
export shape + Content-Disposition attachment header, decide_all
with nothing pending + empty/missing decision → 400, retry of an
unknown task → 404, and the v4 page-landmark sweep — Schedules and
Chat pages and forms, global search, palette, density toggle,
bulk-approval buttons, retry hook, sys-export section).

One test-authoring fix during the run (not a product bug): the
landmark test first asserted `id="sys-export"` with double quotes,
but that element is built by the page's JS with single quotes, so
the assertion false-fired on the raw HTML. The test now checks for
the element name itself; the section was verified present in the
rendered System page either way. (Same trap as v3, same fix.)

Extra check beyond the suite: the **served** page's `<script>`
block (after placeholder substitution — extracting from the .py
source instead checks the unprocessed escapes and false-fails) was
extracted and passed `node --check` — JS syntax OK.

## Demo regression (CLI demo, isolated home)

`GHOST_AGENT_HOME=/tmp/ga-v4-demo PYTHONPATH=src python3 -m ghost_agent demo`
exited **0**, identical shape to v1–v3: paused at waiting_approval
for write_file, approved, all 3 steps done, `demo_output.txt`
content exact ("Ghost Agent demo run\n"), candidate skill proposed
(untrusted), 11 trail events. No regression.

## Live HTTP smoke (real serve, isolated home, port 8774)

- `/` → 200. `POST /api/schedules` (interval 30m) → 201 with a
  computed `next_run_at`; interval 0 → 400 with a plain reason.
- `POST /api/schedules/{id}/run` → 201, created the task, set
  `last_task_id`/`last_run_at` — and the workspace stayed **empty**:
  the scheduled write paused at `waiting_approval`. Then
  `POST /api/approvals/decide_all {approved}` → decided 1, and the
  file landed with exact content ("from schedule").
- Toggle flipped enabled true → false; search `q=smoke` returned
  the schedule's task plus 2 trail hits; `/api/export` → 200 with
  all 9 keys (1 task, 1 schedule, 7 trail events at that point);
  `DELETE /api/schedules/{id}` → 200 and the list emptied.
- **Real thread fire:** a 1-minute interval schedule created over
  the API fired on its own ~62 seconds later (20-second tick
  granularity): `last_task_id` set, `next_run_at` advanced exactly
  one interval from the previously scheduled time, and exactly one
  `schedule_fired` trail event recorded.
- CLI in an isolated home: `schedule-add` (interval + daily),
  `schedules` list, invalid interval → exit 2 with a plain error,
  `schedule-remove` — all correct.

## What changed

- `src/ghost_agent/scheduler.py` — NEW. Schedule validation +
  creation (`create_schedule`, strict 24h HH:MM parsing) and the
  pure `tick(store, agent, now)` engine: enabled + due only, at
  most one fire per tick, next_run advanced from the previously
  scheduled time in whole intervals (daily: next day at HH:MM in
  the tick's timezone) so missed runs are skipped, never
  burst-fired. Firing creates + runs a task through the normal
  agent loop and records a `schedule_fired` trail event.
- `src/ghost_agent/store.py` — generic `delete()` for collections
  (used by schedule removal).
- `src/ghost_agent/server.py` —
  - Endpoints: `GET /api/schedules`; `POST /api/schedules`
    (validation → 400 with plain reasons); `POST
    /api/schedules/{id}/toggle`; `POST /api/schedules/{id}/run`
    (create + run now, updates last_*); `DELETE
    /api/schedules/{id}` (unknown id → 404 everywhere);
    `GET /api/search` (grouped, capped at 8 per group, trail
    newest first, empty q → empty groups); `POST
    /api/approvals/decide_all` (snapshot of pendings, each decided
    via agent.approve_task/deny_task so each is individually
    trailed; returns results + decided/approved/denied counts;
    bad decision → 400); `POST /api/tasks/{id}/retry` (failed
    only → 400 with the current status in the reason; unknown →
    404; new task, same goal, title + " (retry)", original
    untouched); `GET /api/export` (full snapshot as an attachment
    download; store data only, so no token/key can appear).
  - Pages: **Schedules** (create form with interval/daily switch,
    cards with kind badge, next/last run, link to the last task,
    enabled badge in the sidebar nav, toggle / run now / delete
    with confirm) and **Chat** (goal as a user bubble, steps as
    agent cards with args/status/output, inline Approve/Deny
    while waiting, recent-task picker, thread re-polled on every
    dashboard refresh while the page is open).
  - Global: header search with a grouped results dropdown (task
    results jump to the task, trail results jump to Logs with the
    query applied); Ctrl/Cmd+K command palette (all 11 pages +
    New demo task + Export state; filter-as-you-type, Enter runs
    the first match, Esc closes); Approve all / Deny all on the
    Approvals page (confirm first, feedback summary from the
    response); Retry buttons on the Board's failed cards and in
    the Tasks detail; Comfortable/Compact density toggle
    (body.compact class, persisted in localStorage); System page
    gained a State snapshot section (counts in the export shape)
    with a Download state export button.
  - `serve()` starts a daemon scheduler thread ticking every 20
    seconds (first tick immediately). It is started ONLY by
    serve() — importing the module or building a server in tests
    never fires schedules. A failing tick is swallowed so a bad
    schedule cannot take the dashboard down.
- `src/ghost_agent/cli.py` — `schedules`, `schedule-add`
  (--kind/--interval/--time/--title), `schedule-remove`.
- `src/ghost_agent/__init__.py`, `pyproject.toml` — version 0.4.0.
- `README.md` — v4 pages, scheduler behavior (incl. the honest
  "only fires while the dashboard server runs" note), new APIs,
  CLI commands, tests section.
- `tests/test_sanity_gate_v4.py`, `tests/test_server_v4.py` — new.
- `BUILD_REPORT_V4.md` — this file.

## Notes / downgrades

* No findings downgraded; nothing in the spec was cut.
* `decide_all` decides only the approvals pending at call time.
  If approving a step lets a task run into a *later* step that
  also needs approval, that new approval is left pending for a
  human — deliberate, and stated on the Approvals page.
* Daily schedules use the timezone of the tick's `now` (the
  server's local time when serve() drives the ticking), per spec.
* Chat history is client-side session state plus the recent-task
  picker, per spec — no chat-specific backend was added; the
  thread renders from the existing task-detail API.
* The scheduler thread shares the file-backed store with request
  threads (the dashboard already ran tasks from request threads);
  the store's atomic-replace writes keep this safe at dashboard
  scale.
