# Ghost Agent v0.6.0 - Build Report (usage accounting, safe import, search_files, planner guidance)

## Sanity Gate v6 - FIRST, as required

`tests/test_sanity_gate_v6.py`, 5 known answers, written before any
v0.6.0 code and run against the v0.5.0 tree first.

**Pre-implementation run (v0.5.0 code): 1/5** — K1 passed; K2–K5
failed because usage accounting, `/api/import`, `search_files`, and
the append pattern did not exist yet. That is the baseline the work
had to move.

**Final run (v0.6.0 code): 5/5 PASS**

| Case | Known answer | Final |
| --- | --- | --- |
| K1 Back-compat | v1+v2+v3+v4+v5 gates (25 tests) pass in the same run (subprocess) | PASS |
| K2 Usage accounting | Stub provider response with usage {120/30/150} driven through a real task run: task totals, the `model_usage` trail event, and `/api/analytics` all show exactly 150/120/30 attributed to stub-model-x, cost unknown (unpriced model); a MockModel-only run adds 0 real tokens with source "mock" | PASS |
| K3 Import safety | Export from seeded A (trusted memory, trusted skill, enabled schedule, done task) → import into B: B's memory + skill are candidates (trusted lists empty, memory source `import:`-prefixed, verified reset), B's schedule exists but disabled, B's task is archived + flagged imported and never in the live task list, duplicate id re-minted (B's pre-existing record intact); malformed snapshots → 400 with B's counts unchanged; import without token → 401 | PASS |
| K4 search_files | Exact match set with correct line numbers on a seeded workspace; `../` and absolute outside paths cannot escape the root; binary and >1 MB files skipped; 60 matching files cap at 50 results | PASS |
| K5 Append governance | "append line two to notes.txt" plans exactly one `append_file` step, pauses in waiting_approval, file byte-identical before approval, exactly `line one\nline two` after | PASS |

## What v0.6.0 is

1. **Real token/cost accounting.** The OpenAI-compatible adapter no
   longer discards the provider's `usage` block. Each plan call
   produces a usage record (`models.usage_record`): provider calls
   carry the real prompt/completion/total numbers and the response's
   model name; MockModel records zeros with source `"mock"`. The
   Router returns the record; `Agent.create_task` stores cumulative
   `usage_totals` + `usage_by_model` on the task and writes one
   `model_usage` trail event per call. `/api/analytics` gains a
   `usage` block: totals, provider/mock call counts, and a per-model
   breakdown. **Pricing honesty:** cost is estimated only for models
   in the built-in table (`MODEL_PRICING_USD_PER_MILLION`: gpt-4o,
   gpt-4o-mini, gpt-4.1, gpt-4.1-mini; longest prefix wins). Any
   other model reports cost `null` / status `"unknown"` — never a
   guessed number; when some calls are unpriced the total is unknown
   and the priced part is exposed separately as
   `known_estimated_cost_usd`. The Analytics UI now always shows
   tracked tokens and shows cost only when it is known. The
   fallback path is unchanged: provider failure → MockModel, with
   the failure reason recorded exactly as before (the recorded usage
   for such a task is the MockModel call that actually planned it).
   The legacy v2 `tokens_*` payload fields are retained for
   compatibility; `usage` is the authoritative block.
2. **Safe import.** `POST /api/import` accepts an export snapshot.
   `_validate_snapshot` checks the full shape (required lists,
   object entries, ids present and unique per list, per-collection
   field types) **before anything is written**; any failure is a
   400 with the store untouched. Import semantics: memories →
   candidates only (status candidate, verified False, source
   prefixed `import:`, original status kept as
   `imported_from_status`); skills → candidates only (trusted
   False, same source prefixing); schedules → live but **disabled**;
   tasks, approvals, and trail → separate `archived_tasks` /
   `archived_approvals` / `archived_trail` collections, flagged
   `imported` (+ tasks/approvals `archived`), never in the live
   collections, never actionable, and `Agent.run_task` refuses
   imported/archived tasks outright. Duplicate ids are re-minted
   (never overwritten) and internal references (task↔skill,
   approval→task, schedule→task, memory supersede links, trail
   task/detail ids) are remapped to the new ids. Success writes a
   `state_imported` trail event and returns the counts. The System
   page gains an Import control (file picker, client-side JSON
   read, POST, resulting counts shown with the candidate-only rule
   stated in the copy). The token gate applies to import exactly
   like every other `/api` route (401 verified in K3). Export now
   also round-trips the three archived collections.
3. **search_files + planner patterns.** New read tool
   `search_files(query, path=".")`: literal substring search,
   workspace-relative paths with line numbers and matched line
   text, 50-match cap, binary (NUL-sniffed) and >1 MB files
   skipped, base path sandbox-checked and every candidate
   re-checked for containment after resolution (symlink escapes
   skipped). New write tool `append_file` (raw append, no added
   newline) — classified write, so it pauses for approval exactly
   like `write_file`. MockModel patterns: "find <text> in <path>" /
   "search <path> for <text>" → `search_files`; "append <text> to
   <file>" → `append_file`; "summarize <file>" → `read_file` plus an
   `echo_note` that plainly labels the plan as MockModel behavior
   (no fake summaries). Skills proposed from runs record the new
   tools and their permission classes via the existing `tool_kind`
   path. The provider planner prompt's tool list also names
   `search_files` and `append_file`.
4. **Analytics + Overview polish.** Analytics renders the new usage
   block (tokens always; cost only when known; tokens-by-model
   bars). `activity_by_day` now also counts `approvals_decided` per
   day from the approval_granted / approval_denied trail events
   that were already recorded, and the Overview chart draws it as a
   second, amber series with a legend; Analytics keeps the
   single-series chart. Overview and Analytics series remain
   identical payloads (v3 K3 invariant preserved).
5. **Live-run follow-up: planner prompt guidance.** From the first
   live-model run (Muse Spark 1.3 via the Meta API, Ryan-approved):
   the real model planned list_dir → read_file → a shell grep/sed
   one-liner to mechanically extract a "summary" instead of
   composing content with write_file. Governance held; the prompt
   invited the shortcut. `build_planner_prompt` now instructs:
   (a) prefer write_file with content the model composes itself
   when the goal asks it to write or summarize something, and do
   not use shell to extract/assemble composed content;
   (b) use shell only when the goal explicitly asks to run a
   command, or for purely mechanical operations;
   (c) order steps so reads of source files come before any write
   that depends on them. Plan parsing is byte-for-byte the old
   logic. Pinned by unit tests in `tests/test_v6_features.py`:
   the guidance strings are asserted present in the built prompt,
   and a stub-provider run asserts parsing behavior (step order,
   default verify fill, content intact) is unchanged while the
   request that went out carries the guidance.

## Test results

- Gate v6: **5/5** (history above).
- New unit tests: `tests/test_v6_features.py`, **8 passed**
  (prompt guidance + parsing, pricing table incl. longest-prefix
  and unknown→None, usage record shapes, governance classes for the
  new tools, MockModel patterns incl. old patterns untouched,
  raw-append tool semantics, import validation rejects wrong shapes
  and leaves the store untouched).
- Full suite: **94 passed, 0 failed** (`PYTHONPATH=src python3 -m
  pytest tests/ -q`): the 81 pre-existing tests, gate v6 (5), and
  the new v6 unit tests (8).

### Existing-test changes (recorded, none weakened)

1. `tests/test_server_v4.py` and `tests/test_server_v5.py`: version
   pins `"0.5.0"` → `"0.6.0"` (3 assertions). Reason: the
   version-bump deliverable itself; these pin the export/system
   version strings.
2. No other pre-existing test file was modified. In particular the
   v2 analytics test's legacy `tokens_tracked` / `tokens_note`
   assertions still pass because those legacy fields are retained;
   the v3 activity test still passes because `activity_by_day`
   items only gained a field and Overview/Analytics still return
   the identical series.

## Demo and smoke

- CLI demo (isolated home /tmp/ga-v6demo): exit 0; task paused at
  waiting_approval, demo approval granted, 3 steps done,
  demo_output.txt exactly `Ghost Agent demo run\n`, candidate skill
  proposed. Trail events for the demo task: 12 (was 11 in v0.5.0;
  the +1 is the new `model_usage` event — the accounting working as
  designed, not drift).
- Live HTTP smoke (real `serve` process, seeded home with a demo
  task + an approved write task): GET / 200 (78,636 bytes) with the
  System import control (`sys-import`, `import-file`, "Import
  snapshot", candidate-only copy) and the Analytics usage renderer
  present; `/api/analytics` returned the usage block (2 mock calls,
  0 tokens, cost_status `mock_only`) and `activity_by_day` with
  `approvals_decided: 2` for the seeded day; `/api/overview`
  version `0.6.0` with an activity series identical to Analytics;
  malformed import over live HTTP → 400 with the validation reason;
  the icon route still 200 image/jpeg.
- The served page's `<script>` was extracted from the live response
  and passed `node --check`.

## Files changed

- `src/ghost_agent/models.py` — usage records + price table +
  cost estimator, MockModel `last_usage` + find/search/append/
  summarize patterns, planner prompt guidance
  (`build_planner_prompt` / `PLANNER_GUIDANCE`), Router returns
  usage.
- `src/ghost_agent/tools.py` — `search_files` (read) and
  `append_file` (write) + `TOOL_KINDS` entries and search caps.
- `src/ghost_agent/agent.py` — task usage totals/by-model storage,
  `model_usage` trail event per plan call, run_task refuses
  imported/archived tasks.
- `src/ghost_agent/server.py` — `_usage_summary` + analytics
  `usage` block, `activity_by_day` approvals series + grouped
  Overview chart, `_validate_snapshot` / `import_snapshot` +
  `POST /api/import`, export round-trips archived collections,
  System import control + `importState()`, Analytics `usageHtml`,
  glyphs for the new tools.
- `src/ghost_agent/__init__.py`, `pyproject.toml` — version 0.6.0.
- `tests/test_sanity_gate_v6.py` — NEW, gate v6 (5 cases).
- `tests/test_v6_features.py` — NEW, 8 unit tests.
- `tests/test_server_v4.py`, `tests/test_server_v5.py` — version
  pins only (above).
- `README.md` — governance/tools lists, new "Usage accounting,
  import, and search (v0.6.0)" section, Models notes, test list.
- `BUILD_REPORT_V6.md` — this report.

## Honest limits

- One model call per task exists today (the planning call), so
  "cumulative" totals currently aggregate that single call per task
  plus the per-model breakdown across tasks; if mid-run model calls
  are ever added, they must write their own `model_usage` events
  and totals through the same record shape.
- When a provider response is unusable and the Router falls back,
  the failed call's usage (if any) is not recorded — the provider
  returned no parseable plan, and inventing numbers would break the
  honesty rule; the fallback reason is recorded as before, and the
  recorded usage is the MockModel call that actually planned.
- The price table is a small built-in estimate table, not a live
  price feed; rates can drift from providers' current pricing,
  which is why every priced figure is labeled an estimate and
  everything else is unknown.
- Import validation is structural (shape, types, id uniqueness),
  not semantic: a well-shaped snapshot with odd field values
  imports under the same safety transforms (candidate-only,
  disabled, archived), which is where the protection lives.
- search_files matching is literal and case-sensitive by design;
  there is deliberately no regex mode in a read tool that scans
  the whole workspace.

No GitHub push, no publish, $0 spend. Still untested (unchanged):
a second live provider run to observe the new planner guidance and
real usage numbers end-to-end against a real provider — the stub
seam proves the accounting; the live behavior of the guidance is
the next live run's to confirm.
