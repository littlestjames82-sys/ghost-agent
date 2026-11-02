# Ghost Agent v0.9.0 — Build Report

## Sanity Gate v9 output (gate first, per standing rule)

Gate file: `tests/test_sanity_gate_v9.py` (7 tests: K1–K6 plus K7
back-compat). Written FIRST and baselined against the v0.8.0 code
before any implementation:

Baseline on v0.8.0 code (recorded before implementation):

```
K1 budget trips          FAILED — KeyError: 'token_budget'
K2 under budget          FAILED — KeyError: 'token_budget'
K3 budget disabled       FAILED — KeyError: 'token_budget'
K4 fallback facts        FAILED — output carries no file count /
                                mtime / carries list
K5 stored output         FAILED — stored output is 2000 chars,
                                not the full 5000
K6 /api/system budget    FAILED — KeyError: 'token_budget'
(K7 back-compat was not part of the baseline run; it re-runs the
 prior gates against the NEW code by design.)
```

Gate on v0.9.0 code:

```
K1–K6 (run directly):  6 passed in 0.67s
Whole gate file (incl. K7's subprocess re-run of gates
v1..v8, 51 tests):     7 passed in 290.32s
```

Full suite (115 prior + 7 new gate tests = 122):

```
122 passed in 588.03s
```

What each gate case proves:

- **K1** — with a scripted provider charging 60,000 tokens per
  call against the default budget: the first call (60,000) is
  fine; the second brings the task to 120,000 ≥ 100,000 and the
  task fails *there* — error exactly `token budget reached
  (120000 of 100000 tokens)`, one `budget_exceeded` trail event
  carrying `used`/`budget`, exactly 2 provider calls (no third),
  only the first step executed, `skill_id` null and no skill in
  the store, and the full 120,000 still accounted in
  `usage_totals`.
- **K2** — a small-usage stub (15 tokens/call) completes
  normally; the record carries `token_budget: 100000`; no
  `budget_exceeded` event exists.
- **K3** — `GHOST_AGENT_TOKEN_BUDGET=0` with the same fat stub
  (3 calls × 60,000 = 180,000 tokens) runs to its normal end:
  status done, `token_budget` null, no budget event.
- **K4** — a non-git fixture tree (README.md, notes.txt,
  sub/deep.txt, deep.txt carrying a known newest mtime) answers
  with `3 file(s)`, the newest mtime in ISO form, and
  `carries: README.md`, while claiming no git data
  (`is_git_repo` false, branch/last_commit null, no "branch" in
  the output); a marker-less tree answers `carries: none of the
  usual markers`; the git fixture still returns its real branch
  and last-commit subject, byte-for-byte the v0.8.0 shape.
- **K5** — a 5,000-char tool result is stored whole on the task
  record (`len > 4000` and exactly equal to the source text);
  the loop's history constants are untouched.
- **K6** — `GET /api/system` reports `token_budget: 100000`
  under a default environment.
- **K7** — all 51 prior known-answer gate tests (v1–v8) pass in
  the same run under the new code; nothing regressed underneath.

## What v0.9.0 is

**Three evidence-based fixes, each traced to a Nov 2 live run:**

1. **A per-task token budget guardrail** (`agent.py`). The
   10-goal gauntlet on v0.7.0 grounded everything, at a price:
   document-heavy goals cost 35k–45k tokens each because the
   loop re-sends its history on every call, and nothing in the
   runtime could stop a run from spending on. Now
   `GHOST_AGENT_TOKEN_BUDGET` caps a task's total recorded
   provider tokens — integer, **default 100000**, **0 or negative
   disables**, unparseable values fall back to the default
   (`configured_token_budget()` in `agent.py`, resolved once at
   Agent construction and stamped onto every task record as
   `token_budget`, null when disabled; `GET /api/system` reports
   the configured value). Enforcement is in the loop: after each
   provider call's usage is recorded, if the total has reached
   the budget and the model still has a step to run, the task
   fails *before* that step executes and before any further
   model call — `_fail_budget` writes status `failed`, the error
   `token budget reached (<used> of <budget> tokens)`, and a
   `budget_exceeded` trail event; the skill proposal at the end
   of a successful run is never reached. The same check runs in
   `create_task`, so a single huge first answer fails the task
   before any tool executes. A call that answers "done" is not
   tripped: the work finished rather than being cut off.
2. **`repo_status` tree facts for non-git trees** (`tools.py`).
   The ecosystem probes showed most studio trees are API-synced
   copies without `.git`, so v0.8.0's "not a git repository" was
   the whole answer for most of the studio. The non-git branch
   (which is also where *any* git failure lands — missing binary,
   timeout, nonzero exit) now adds `_tree_facts`: a bounded
   recursive scan reporting the **file count** (counting stops
   just past 10,000 and renders `10000+`), the **newest file
   modification time** in ISO form (the mtime scan is bounded
   with the count), and a **`carries:` list** of root markers —
   the first `README*` file, then `BUILD_REPORT.md`,
   `pyproject.toml`, `package.json` — or `carries: none of the
   usual markers`. The result also carries the facts as fields
   (`file_count`, `file_count_capped`, `newest_mtime`,
   `markers`). The git-repo output string is exactly v0.8.0's.
3. **Fuller stored step outputs** (`agent.py`). The stored
   per-step output cap rises from 2,000 to 8,000 chars
   (`STEP_OUTPUT_CHARS`), so a 72-project `list_projects` result
   is legible in the task detail instead of showing ~15 lines.
   The loop's history budget (4,000 per output / 40,000 total)
   is a different budget and is **not** changed.

Versions: `__version__` and `pyproject.toml` both 0.9.0;
`/api/system` reports 0.9.0.

**Constraint honored:** the dashboard PAGE string in `server.py`
was not touched — an Android build is syncing the page from this
tree concurrently. The only `server.py` change is one computed
JSON field (`token_budget`) in `system_payload`. The v5/v8 page
checks (drawer contents, `node --check`) pass unmodified in the
suite, confirming the page is intact.

## Verification beyond the gate

- **Live `repo_status` spot checks** against the real registry:
  ghost-agent (API-synced tree) → *"local tree at
  /home/hatch/workspace/ghost-agent is not a git repository -
  86 file(s), newest modified 2026-11-02T06:32:55+00:00 -
  carries: README.md, BUILD_REPORT.md, pyproject.toml"*;
  ghostcut (real clone) → *"branch main - 256 uncommitted
  change(s) - last commit cf5e79e9 (2026-05-17) docs: readme"* —
  identical to the v0.8.0 spot check, as required.
- **Budget resolution probe**: unset env → 100000;
  `GHOST_AGENT_TOKEN_BUDGET=-5` → None (disabled).
- **CLI demo, isolated home** (`GHOST_AGENT_HOME=/tmp/ga-v9demo`):
  exit 0; `demo_output.txt` byte-identical; same candidate
  skill; trail exactly **19 events** (unchanged — MockModel
  records zero usage, so the budget adds no events and never
  trips the demo).

## Recorded changes to existing tests (with reasons)

- `tests/test_server_v4.py` (1 line) and `tests/test_server_v5.py`
  (2 lines): version pins `"0.8.0"` → `"0.9.0"`. Reason: the
  `/api/system` and export version assertions must name the
  current version — the same recorded pin update v0.6.0, v0.7.0,
  and v0.8.0 made.
- No other existing test was modified. All 115 prior tests pass
  unmodified — including the v7 gates' exact usage assertions
  (stub usages are far below the default budget) and the CLI
  demo's 19-event trail.

## Honest limits

- **The budget counts recorded usage, not ground truth.** A
  provider that under-reports its `usage` block under-counts the
  spend the budget sees. And because the check happens after a
  call's usage lands, a single call can overshoot the budget —
  by at most that one call. The guardrail bounds a run's cost;
  it does not make any individual call cheap.
- **The budget guards provider spend only.** MockModel tasks
  record zero usage and can run forever under it (bounded by the
  step cap instead). Local, free work was never the problem the
  gauntlet exposed.
- **The budget is process configuration, not a per-task
  choice.** It resolves from the environment when the Agent is
  constructed; changing it means a new process (or a new Agent).
  A task keeps the budget stamped on its record even if the
  environment changes mid-flight — which is the honest behavior:
  the record says what the task actually ran under. Tasks
  imported from older snapshots carry no `token_budget` and run
  unguarded.
- **Tree facts are a bounded sample when capped.** Past 10,000
  files the count is `10000+` and the "newest" mtime is the
  newest *among the files scanned before the cap*, not a proven
  tree-wide maximum. Markers are root-level only and the README
  match is a case-sensitive `README` prefix; unreadable files
  are counted but contribute no mtime.
- **8,000 chars is a cap, not a promise of completeness.** A
  `list_projects` output for 72 projects lands close to it;
  longer tool outputs still truncate in the record (the tool's
  own full result is unchanged, and the Ecosystem page renders
  the registry in full). History truncation is untouched, so
  what the *model* sees is exactly what v0.7.0/v0.8.0 showed it.
- **Suite runtime grew again**, to ~10 minutes: the gates nest
  subprocess back-compat runs three levels deep now (v9's K7
  runs v8, whose K8 runs v1–v7b). The cost of the standing
  gate-discipline, paid in wall-clock, not correctness.

## Files changed

- `src/ghost_agent/agent.py` — `configured_token_budget()` +
  `TOKEN_BUDGET_ENV`/`DEFAULT_TOKEN_BUDGET`; the Agent resolves
  and stamps `token_budget` on every task; enforcement in
  `create_task` (huge first answer) and in the `run_task` loop
  (after each call's usage lands, before the proposed step
  executes), via `_budget_tripped` / `_budget_reason` /
  `_fail_budget` with the `budget_exceeded` trail event;
  `STEP_OUTPUT_CHARS = 8000` replaces the inline 2,000-char
  stored-output cap.
- `src/ghost_agent/tools.py` — `_tree_facts` (bounded count,
  newest mtime, root markers) wired into `repo_status`'s
  non-git branch; `TREE_COUNT_CAP` / `ROOT_MARKERS` constants;
  the git-repo output is byte-identical to v0.8.0.
- `src/ghost_agent/server.py` — `system_payload` gains the
  computed `token_budget` JSON field. **The PAGE string is
  untouched** (concurrent Android page sync).
- `src/ghost_agent/__init__.py`, `pyproject.toml` — 0.9.0.
- `tests/test_sanity_gate_v9.py` — new gate (7 tests).
- `tests/test_server_v4.py`, `tests/test_server_v5.py` — version
  pins (recorded above).
- `README.md` — "Token budget, tree facts, fuller records
  (v0.9.0)" section, plus the Tests list and build-report links.

No push, no publish — per the brief.
