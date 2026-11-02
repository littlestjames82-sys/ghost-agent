# Ghost Agent v0.7.0 — Build Report

## Sanity Gate v7 output (gate first, per standing rule)

Gate file: `tests/test_sanity_gate_v7.py` (9 tests: K1, K2, K3,
K4-loop, K4-cap, K5, K6, K7-corrects, K7-cap). Written FIRST and
baselined against the v0.6.0 code before any implementation:

Baseline on v0.6.0 code (recorded before implementation):

```
K1 (subprocess re-run of gates v1..v6, 30 tests)  PASSED (72.07s) — as expected
K2 grounding loop                                  FAILED — as expected
K3 approval inside the loop                        FAILED — as expected
K4 loop detection                                  FAILED — as expected
K4 step cap                                        FAILED — as expected
K5 mid-loop provider failure                       FAILED — as expected
K6 MockModel regression + muse-spark pricing       FAILED — only on the new
                                                     muse-spark pricing asserts;
                                                     the MockModel regression
                                                     parts already passed
K7 validation feedback (corrects)                  FAILED — as expected
K7 validation feedback (retry cap)                 FAILED — as expected
```

Gate on v0.7.0 code:

```
K2–K7 (8 tests, run directly):  8 passed in 0.12s
Whole gate file (incl. K1):     9 passed in 72.71s
```

Full suite (94 prior + 9 new gate tests = 103):

```
103 passed in 151.33s
```

What each gate case proves:

- **K1** — all 30 prior known-answer gate tests (v1–v6) pass in
  the same run as the new code; nothing regressed underneath.
- **K2 grounding** — the loop's reason to exist. A scripted stub
  provider answers read_file for a seeded file, then ASSERTS its
  second call's prompt contains the seeded file's actual output
  text (`QUOKKA-7391 distinctive seeded line`), then answers a
  write_file quoting it, then done. The written file contains the
  seeded text; usage totals equal the exact sum of the stub's
  per-call usage (33/14/47 across 3 calls); 3 `model_usage` events;
  `replan` iterations [0, 1, 2].
- **K3 approval inside the loop** — read → write → done: the write
  pauses in `waiting_approval` with the file absent; the stub's
  call count is FROZEN while paused, including across a redundant
  run_task call (no model call happens during an approval wait);
  approve resumes the loop to done.
- **K4 loop safety** — a stub proposing the identical step forever
  fails with "loop detected" after 2 executed steps (well under
  the cap); a stub proposing fresh steps forever fails at exactly
  12 executed steps with "step cap reached" in the trail
  (`loop_guard` event) and no skill proposed.
- **K5 mid-loop provider failure** — first call succeeds (a read),
  the second raises: the task FAILS with the provider reason in
  the trail (`model_call_failed`), `model` stays
  `openai-compatible`, `fallback_reason` stays None, no skill —
  the brain never changes mid-task.
- **K6 MockModel regression + pricing** — demo, write, and find
  tasks through MockModel produce byte-identical files, identical
  statuses and skill proposals, zero usage, `fallback_reason`
  None. muse-spark: `estimate_cost_usd("muse-spark-1.3", 1M, 1M)`
  = $5.50 exactly (1.25 + 4.25), and a muse-spark usage record
  reports `cost_status: "estimated"`.
- **K7 validation feedback** (added from the live gauntlet
  evidence, see below) — a step with wrong argument names
  (`search_files` with `text` instead of `query`) is REJECTED,
  not fatal: the validation error text is in the next call's
  history (the stub asserts it), the corrected step runs, the
  task completes; `step_rejected` trail event carries the reason.
  A stub that never corrects fails after the 2-retry cap (3 calls
  total) with the validation error as the task error and in the
  trail.

## What v0.7.0 is

**Plan-act-observe planning** replaces v0.6.0's single-shot plan.
The agent loop now asks the model for the NEXT step given the
goal plus the history of completed steps — tool, args, status,
and observed output (each output truncated to 1,000 chars; total
history capped at ~8,000 chars, oldest dropped first). The model
answers one step object or `{"tool": "done"}`. A step is
therefore planned with the ACTUAL outputs of the steps before it
in view: a write can use what its reads found, which is the exact
weakness the v0.6.0 first live run exposed (a "summary" composed
before the source was read came out as a generic extraction).

Supporting pieces:

- **Guards.** `MAX_STEPS = 12` executed steps per task; hitting
  the cap without a done fails the task with "step cap reached"
  in the trail. Identical (tool + args) re-proposal as the last
  executed step, twice in a row, fails with "loop detected".
- **Governance unchanged, per step.** A proposed write/exec step
  is recorded and pauses the task in `waiting_approval` BEFORE
  executing; the loop resumes after approval; deny fails the task
  as before; record-before-execute (trail write/flush/fsync
  before the tool runs, failure = no execution) is untouched.
- **Fallback honesty.** Only the FIRST next-step call may fall
  back to MockModel (reason recorded, as always). A later call
  failing fails the task with the provider reason in the trail.
  `task.model` always names the brain in charge.
- **Per-call usage.** Every model call records its `model_usage`
  event and accumulates into `usage_totals` / `usage_by_model`
  with the same honesty rules as v0.6.0 (any unpriced provider
  call makes the total "unknown", never a partial sum). `calls`
  now exceeds 1 for real tasks — that is the loop working.
- **Trail.** A `replan` event per next-step request (iteration
  number), plus `loop_guard`, `model_call_failed`,
  `step_rejected`, and `validation_failed` events for the new
  failure/feedback paths — all visible in Logs.
- **MockModel through the loop.** `MockModel.next_step` serves
  its existing deterministic full plan one step at a time by
  history length, then done. Every v0.6.0 pattern produces the
  same steps and outcomes (K6). Providers that ignore the
  one-step instruction and answer with a full plan array are
  served stepwise from the buffered array; usage is recorded
  once, when the array was fetched (no phantom calls, no
  double-counted tokens). Router also keeps a legacy shim:
  adapters exposing only `plan()` are served stepwise the same
  way, and `Router.plan` itself is unchanged.
- **Dashboard compatibility.** Tasks grow their step list as the
  loop runs; Board/Tasks/Chat read the steps array as-is
  (`steps_total` grows). No page changes. Verified live (below).

**Gauntlet-driven additions** (live 10-goal gauntlet on
v0.6.0 + Muse Spark, Nov 2, 2026: 3 of 10 goals died on
`missing args for search_files: ['query']` — the model invented
an argument name because the prompt never gave one):

1. **Exact argument signatures in every planner prompt.**
   `TOOL_SIGNATURES` — `read_file{path}, list_dir{path},
   echo_note{text}, search_files{query, path?},
   write_file{path, content}, append_file{path, content},
   shell{command}` — now appears in both the legacy planner
   prompt and the next-step prompt.
2. **Validation feedback instead of death.** A proposed step
   whose args fail validation (`tools.validate_args`, same error
   wording the dispatcher raises) is parked as a `rejected` step;
   its error rides the history into the next call so the model
   can correct itself. Cap: 2 correction retries per task; past
   that the task fails honestly with the validation error as its
   error and in the trail. Unknown tools are NOT validation
   feedback — they stay on governance's deny-by-default path.
   Validation runs before any approval prompt, so a step that
   cannot run never consumes a human decision.

**Micro-item: muse-spark pricing.** `"muse-spark": (1.25, 4.25)`
added to `MODEL_PRICING_USD_PER_MILLION` (Meta's published rates,
connector doc Oct 26, 2026). The existing longest-prefix rule
covers `muse-spark-1.3` and its MoE contributor variants, which
now report estimated costs instead of "unknown".

Versions: `__version__` and `pyproject.toml` both 0.7.0;
`/api/system` reports 0.7.0.

## Verification beyond the gate

- **CLI demo, isolated home** (`GHOST_AGENT_HOME=/tmp/ga-v7demo`):
  exit 0; demo pauses at the write approval, completes after the
  scripted approval; `demo_output.txt` is byte-identical
  (`Ghost Agent demo run\n`); candidate skill proposed with the
  same tools list. Trail is 19 events (v0.6.0: 18) — the loop's
  added `replan` events, as the brief allows.
- **Live HTTP smoke** (seeded home, MockModel): create a write
  task → auto-run pauses at `waiting_approval` with 1 step →
  approve → done with `plan_calls: 2`, `planning_done: true`;
  Board shows it in Done; task detail shows the grown steps;
  trail reads `replan, model_usage, task_created,
  approval_requested, approval_granted, tool_execute, step_done,
  replan, model_usage, skill_candidate, task_done` (record
  before execute, in order); `/api/system` version 0.7.0; the
  served page's script passes `node --check`.

## Recorded changes to existing tests (with reasons)

- `tests/test_server_v4.py` (1 line) and `tests/test_server_v5.py`
  (2 lines): version pins `"0.6.0"` → `"0.7.0"`. Reason: the
  `/api/system` version assertion must name the current version —
  the same recorded pin update v0.6.0 made.
- No other existing test was modified. All 94 v0.6.0 tests pass
  unmodified, including the v6 gate's exact single-call usage
  assertions (the provider adapter's buffered-plan serving keeps
  them true) and the router fallback test (the Router's legacy
  `plan()` shim keeps adapter doubles working).

## Honest limits

- The loop is only as good as the model's next-step answers. A
  weak model can still propose poor steps; the guards bound the
  damage (12 steps, loop detection, validation cap), they do not
  make the steps good.
- History truncation is mechanical: outputs past 1,000 chars and
  history past ~8,000 chars are cut oldest-first. A task whose
  crucial evidence scrolls out of the window can still plan
  ungrounded late steps — bounded, not solved.
- Validation feedback covers missing/wrong argument names and
  non-object args. Semantically wrong but well-formed args (a
  path that does not exist, a query with no matches) still run
  and fail or succeed on their own merits, as before.
- If a task's brain is the real provider and a LATER process
  without the API key resumes it (e.g. a CLI approve in a bare
  env), the loop's next call fails honestly and the task fails
  with that reason recorded — it never silently drops to
  MockModel mid-task. Approving from the dashboard process (which
  holds the key) is unaffected.
- Live-provider behavior of the loop itself is proven against
  scripted stubs at the urlopen seam, not against a live provider
  in this build (no spend authorized for v0.7.0). The gauntlet
  that motivated K7 should be re-run on v0.7.0 before claiming
  the fix live.
- Rejected steps stay visible in the task's step list (status
  `rejected`) — deliberate transparency; the dashboard counts
  them in `steps_total` but not `steps_done`.

## Files changed

- `src/ghost_agent/agent.py` — plan-act-observe loop
  (create/run rework, history builder, usage merge, guards,
  validation feedback), governance/execution semantics preserved.
- `src/ghost_agent/models.py` — `next_step` on MockModel and the
  OpenAI-compatible adapter (with buffered-plan serving), Router
  loop interface + legacy shim, `TOOL_SIGNATURES` in both planner
  prompts, `build_next_step_prompt`, muse-spark pricing.
- `src/ghost_agent/tools.py` — `validate_args` (shared with the
  loop; error wording matches the dispatcher).
- `src/ghost_agent/__init__.py`, `pyproject.toml` — 0.7.0.
- `tests/test_sanity_gate_v7.py` — new gate (9 tests).
- `tests/test_server_v4.py`, `tests/test_server_v5.py` — version
  pins (recorded above).
- `README.md` — plan-act-observe section (how it works, caps,
  fallback honesty, validation feedback, per-call usage,
  muse-spark pricing); Models + Tests sections updated.

No push, no publish — per the brief.


## Addendum — live gauntlet fixes (pre-push, same night)

The 10-goal live gauntlet (Muse Spark) was run against v0.6.0
(baseline) and then against this build. The baseline scored 6
ungrounded completions, 1 template failure, 1 timeout-fallback
pseudo-completion, and 3 failures on a planner arg-schema bug.
The loop's first live run grounded every goal it finished, and
exposed four defects that are fixed in this tree before push:

1. **Null provider content crashed task creation** (goal 10:
   Spark answered with `content: null`; `content.find` raised
   AttributeError out of `create_task`). Both response parsers
   now raise ModelError("provider returned an empty response"):
   fallback with the reason recorded on a first call, honest
   failure on a later call. Regression gate: v7b K1/K2.
2. **30s provider timeout too tight** — hit in the baseline
   (goal 4) and twice mid-loop (goals 2, 7). Raised to 90s in
   both request paths; timeouts still fail honestly after that.
3. **Step-cap thrash on multi-document goals** (goals 3, 5, 8:
   12 steps of re-reads, ~25k tokens each). Root cause found in
   `_history`: over-budget entries were dropped whole, so the
   model could not see it had already listed/read a file and
   repeated it. History now elides the oldest OUTPUTS first
   (step, args, and status always stay visible) and drops whole
   entries only if the skeleton alone exceeds budget; budgets
   raised (4,000 chars per output, 40,000 total). Regression
   gate: v7b K4.
4. **Loop-efficiency prompt**: the next-step prompt now says not
   to re-read what history already shows, to track gathered
   facts, and to answer done as soon as the deliverable exists.
   Gate: v7b K3.

Final gauntlet state on this tree: **10 of 10 goals completed,
all grounded** (spot-checked: the metrics table reproduces the
real per-version test counts 27/42/56/72/81/94/103; the
inventory carries the real version, entry point, and zero
dependencies; no placeholder brackets anywhere). Honest cost
note: grounded synthesis is not free — the four document-heavy
goals cost 35k-45k tokens each because the loop re-sends its
history per call; the full 10-goal set totals ~210k tokens
(~$0.90 at published Spark rates) versus the baseline's ~21k
tokens for mostly ungrounded output. Total gauntlet spend
across all runs tonight (baseline + loop + retries): ~464k
tokens, roughly $2.10 of the Meta credits.
