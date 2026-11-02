# Ghost Agent v2 — Dashboard Restyle Build Report

Built Nov 1, 2026 (v0.2.0). Local build + verification only: no GitHub
push, no publish, no release, $0 spend. Python stdlib only, zero
dependencies, no build step. All v1 behavior and APIs preserved;
record-before-execute and governance semantics unchanged.

## Sanity gate v2 (known answers) — run FIRST, reported separately

Command: `PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v2.py -v`

**Pass rate: 5/5 (100%)**

| Case | Known answer | Result |
|---|---|---|
| K1 Back-compat board shape | GET /api/board = 4 columns + pending_approvals on seeded data | PASS |
| K2 Fail-closed bind | serve on 0.0.0.0 without GHOST_AGENT_DASHBOARD_TOKEN refuses (non-zero exit, nothing served) | PASS |
| K3 Token gate | with token set: /api/board no token → 401, wrong token → 401, header → 200, cookie → 200, page itself → 200 | PASS |
| K4 Trail search | ?q=<unique marker> returns only matching events; ?q=<absent> returns empty; ?type= filters exactly | PASS |
| K5 Memory action over HTTP | candidate proposed via store, POST /api/memory/{id}/approve → trusted; unknown id → 404 | PASS |

Gate history, honestly: the gate was written before the v2 code and
run once against the v1 codebase to prove the gate itself can fail —
that run was **1/5** (K1 passed, as it must: it pins v1 behavior;
K2–K5 failed because v1 has no fail-closed bind, no token gate, no
trail search, and no memory-action endpoint). Those results were
discarded. After the v2 implementation, the gate was re-run from
scratch: **5/5**. The v1 gate also still passes (4/4; both gates
together: 9 passed).

## Full test suite

Command: `PYTHONPATH=src python3 -m pytest tests/ -q`

**42 passed, 0 failed** = 27 v1 tests (all still passing, unchanged)
+ 5 v2 gate tests + 10 new v2 tests (`tests/test_server_v2.py`:
overview payload + endpoint, analytics aggregates incl. the honest
token/cost note, trail newest-first + limit, memory reject + skill
promote endpoints, system payload secret hygiene, fail-closed serve
unit, non-loopback-with-token start, v2 page structure — all 9 page
sections, sidebar, warning, login box, 5s auto-refresh, hash routing).

One test-authoring fix during the run (not a product bug): the system
payload secret test originally used the token value `"tok"`, which is
a substring of the legitimate field names `token_required` /
`token_env`, so the leak assertion false-fired. The test now uses a
distinctive secret value; the payload itself was verified to contain
only the key/token *presence* booleans.

## Demo regression (v1 demo, isolated home)

`GHOST_AGENT_HOME=/tmp/ga-v2-demo PYTHONPATH=src python3 -m ghost_agent demo`
exited **0**: paused at waiting_approval for write_file, approved,
all 3 steps done, `demo_output.txt` content exact, candidate skill
proposed (untrusted), 11 trail events. No regression.

## Live HTTP smoke (real serve, seeded with the demo run)

All **200**: `/`, `/api/board`, `/api/overview`, `/api/analytics`,
`/api/system`, `/api/memory`, `/api/skills`, `/api/trail?q=tool_execute`,
`/api/trail?type=task_done`. Page HTML contains the sidebar and all
page sections. Overview payload: 1 task done, 1 candidate skill,
11 trail events. System payload: version 0.2.0, adapter `mock`,
`api_key_present: false`, auth mode `none (loopback only)`.

Token mode, live: with `GHOST_AGENT_DASHBOARD_TOKEN` set on loopback —
`/api/board` without token **401**, with `X-Ghost-Token` header
**200**, with `ghost_token` cookie **200**, wrong token **401**,
page `/` **200** (so the token form can be used). Refusal, live:
`serve --host 0.0.0.0` without a token exits **2** with
"refusing to start: host '0.0.0.0' is not loopback and
GHOST_AGENT_DASHBOARD_TOKEN is not set…". (One smoke attempt before
this was discarded: it ran from the wrong directory so the module was
not importable and nothing served; re-run correctly from the repo
root, results above.)

## What changed

- `src/ghost_agent/server.py` — rewritten: sidebar SPA (Overview,
  Board, Approvals, Tasks, Memory, Skills, Analytics, Logs, System;
  hash routing; collapses to a top bar under 820px), status badges
  (running pulses), Ghost violet dark polish, header status bar
  (version, model, pending-approval count, auth mode), token login
  box; new endpoints `/api/overview`, `/api/analytics`,
  `/api/system`, filtered `/api/trail` (q/type/task_id/limit,
  newest first), `POST /api/memory/{id}/approve|reject`,
  `POST /api/skills/{id}/promote`; fail-closed bind rule.
- `src/ghost_agent/cli.py` — serve catches the refusal and exits 2
  with the reason on stderr.
- `src/ghost_agent/__init__.py`, `pyproject.toml` — version 0.2.0.
- `README.md` — v2 pages, API additions, auth-gate behavior.
- `tests/test_sanity_gate_v2.py`, `tests/test_server_v2.py` — new.
- `BUILD_REPORT_V2.md` — this file.

## Notes / downgrades

* No findings downgraded. Analytics invents nothing: tasks record no
  token/cost fields, so the payload and the page say
  "token/cost not tracked yet" instead of showing numbers.
* `/api/trail` with no parameters now returns newest first (v2 Logs
  semantics, per spec). The v1 suite did not pin ordering and still
  passes; the per-task trail inside `GET /api/tasks/{id}` keeps its
  original chronological order.
* The token is a single shared secret (header or cookie), not user
  accounts — stated in the README honest-limits section.
