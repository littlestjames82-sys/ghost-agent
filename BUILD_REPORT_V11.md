# BUILD REPORT — Ghost Agent v0.11.0 "Ghost Intelligence"

The intelligence pass on top of v0.10.0 (profiles/router,
verified: gates 77/77, suite 141/141, pushed). Python stdlib
only, zero dependencies, local-first, $0. Built and verified
locally; **not pushed** (the parent verifies and pushes). No
push script was run; no git/GitHub write of any kind was made.

## What shipped

* **Memory-informed planning (`agent.py`).** At task creation
  the agent selects TRUSTED memories whose text shares keyword
  overlap with the goal: tokenized on non-alphanumerics,
  lowercased, a ~55-word stopword set and tokens <3 chars
  dropped, score = distinct shared tokens, keep score>=1,
  top 5, ordered score-desc then store order. The chosen ids
  ride on the task record (`memory_context`); their texts are
  rendered into EVERY planner prompt (the creation call and
  every loop call) as a "Relevant memory (trusted,
  user-approved):" block, re-rendered per call from the
  trusted store so a record that leaves the trusted set drops
  out. Exactly ONE `memory_recalled` trail event is emitted,
  at first use, and only when the list is non-empty — a task
  with no matches produces no new trail events, and the CLI
  demo in a fresh home still produces exactly 19 trail events
  (gate-pinned). Candidates NEVER reach a prompt.
* **Trusted skills as planning hints (`agent.py`).** The same
  selection pass picks TRUSTED skills (top 2) whose name — or
  source goal, resolved from the record's own `goal` field
  when present or via its `from_task` task — overlaps the
  goal. The prompt gains "Relevant skill '<name>' (trusted):
  a previous successful run used these tools in order:
  t1 -> t2 -> ..." (the skill's ordered `tools` list, falling
  back to the tool order of its stored `steps`). Ids ride on
  the task (`skill_hints`), one `skill_hinted` trail event.
  It is a hint; the planner may deviate. Candidates never hint.
* **Verifier pass (opt-in).** `Agent.create_task(verify=...)`,
  `POST /api/tasks {"verify": true}`, `ghost-agent demo
  --verify`, and a "Verify result" checkbox in the Chat
  composer. When a verify task reaches done, ONE extra call
  goes through the SAME provider/profile that ran the task
  (new `Router.complete` + adapter `complete()` methods; no
  strategy resolution, no fallback). The prompt carries the
  goal and the deliverable — the current workspace contents
  of every file the task's executed write_file/append_file
  steps targeted (each capped 4000 chars, total 12000), or
  the executed steps' outputs when it wrote none — and
  demands a strict JSON verdict, parsed with the existing
  lenient extraction. Recorded on the task as `verification`
  plus a `verification` trail event:
  `{"outcome": "grounded"|"issues"|"unparseable"|"error",
  "issues": [...], "provider": <name>}` — issues recorded
  verbatim. A MockModel-owned task makes NO call and records
  `{"outcome": "skipped", "reason": "mock model"}`. A
  provider error records outcome "error" (the error string
  rides in the trail event's detail). The verdict NEVER
  changes the task's status. The verifier's usage is recorded
  like any other call (task totals + `model_usage` event);
  if it carries the task past its token budget after the work
  was done, the record gains `"over_budget": true` and the
  task still stays done.
* **Tier-aware routing (`providers.py`, `models.py`).**
  Profiles gain an optional `tier` ("frontier" | "standard" |
  "small"). Presets: meta frontier, anthropic frontier;
  openai, openrouter, fireworks, groq, together, gemini
  standard; ollama, lmstudio small. `Providers.effective_tier`
  = the file's tier when valid, else the preset tier for a
  same-named profile, else None — the merge happens at read
  time, so a v0.10.0-shaped providers.json (no tier keys)
  ranks correctly and the user's file is NEVER rewritten
  (gate asserts the file is byte-identical after load). An
  invalid tier is ignored for selection but shown raw in
  /api/system as `tier_configured` (beside the effective
  `tier`). New strategy "best": among CONFIGURED profiles,
  the highest effective tier wins (frontier > standard >
  small > untiered), ties break by stored profile order;
  no configured profiles -> behaves as "default".
  Unknown strategy strings still coerce to "default".
* **Training-data export (Level 2 on-ramp).**
  `training_jsonl()` in `server.py`, served at
  `GET /api/export-training?include_failed=1` (attachment
  `ghost-agent-training.jsonl`) and as
  `ghost-agent export-training [--out PATH]
  [--include-failed]`. One JSONL line per DONE task (failed
  only with the flag, status on the line):
  {task_id, goal, title, provider, model, status,
  created_at, steps: [executed only — done/failed —
  {tool, args, status, output <=2000 chars}], approvals:
  [{tool, decision}] recovered from the trail's
  approval_granted/approval_denied events, usage_totals}.
  The CLI prints a data card after writing (counts by status
  and provider, total tokens); without --out the JSONL goes
  to stdout and the card to stderr so stdout stays
  pipe-clean. The export is built from task records + trail
  only: no providers config, no memory/skill records, no key
  values (gate asserts the fixture secret and the config
  field names are absent from the bytes).
* **Naming + docs.** The dashboard System page's providers
  block is headed "Ghost Intelligence — models & routing"
  and each profile row gains a tier pill; the System page
  also gains a "Download training data (JSONL)" button beside
  the state export. README gains a "Ghost Intelligence"
  section mapping the levels: Level 1 shipped (router +
  recall + hints + verifier + tiers), Level 2's on-ramp is
  the export (dataset first, training later — the Ghost
  Hands planner precedent: trained from a curated 718-example
  dataset), Level 3 parked by decision. Drawer still exactly
  12 pages; page script passes `node --check`.
* **Prompt plumbing (back-compat held).** Planner prompts
  accept an optional `context` (appended after the goal;
  empty context leaves prompts byte-identical to v0.10.0).
  The router threads context only into adapters that declare
  a `context` parameter (signature-checked), so earlier
  adapter doubles keep their two-argument seam; MockModel
  accepts and ignores it. All v7/v7b stub seams, Router seams,
  and providers seams keep working unmodified.

## Gate and suite results

* Gate written FIRST (`tests/test_sanity_gate_v11.py`, 20
  tests) and baselined on the v0.10.0 tree: 16 feature tests
  failed for the designed reasons (no `verify` kwarg, no
  `memory_context`, no `training_jsonl`, no tier fields,
  "best" coerced to "default", 404 on /api/export-training);
  3 passed on the old code by design (the 19-event demo pin,
  verify-off absence, and best-with-nothing-configured —
  where old coercion and new "default" behavior coincide);
  the prior-gates subprocess test was excluded from the
  baseline run.
* Gate cases: I1 trusted memory in EVERY planner prompt + a
  stronger-overlap CANDIDATE memory absent from prompts and
  from `memory_context` + exactly one `memory_recalled`
  event; I2 zero-overlap trusted memory -> no block, no
  event; fresh-home CLI demo trail == 19 events; I3 trusted
  skill's tool sequence in the prompt + `skill_hinted`,
  stronger candidate skill absent; I4 verifier grounded
  (usage calls == 3 incl. the verifier's), issues verbatim,
  unparseable, mock skip with ZERO network calls, provider
  error keeps the task done, verify-off has no field and no
  events, and the HTTP `verify` flag end to end; I5 "best"
  picks frontier over standard over small regardless of
  stored order, untiered ranks last, ties break by stored
  order, none configured -> mock via default behavior, the
  tier merge from a v0.10.0-shaped file (file untouched),
  file tier beats preset tier, invalid tier raw in
  /api/system; I6 export line shape (exact executed-step
  list, approvals from the trail, exact data card), failed
  excluded by default / included marked with the flag, no
  config blob or fixture secret in the bytes, HTTP endpoint
  and CLI parity; I7 /api/system effective tiers for the
  seeded presets, page contains "Ghost Intelligence" + the
  verify checkbox + exactly 12 drawer tiles; P11 prior
  gates v1..v10 in a subprocess (77 tests).
* **Gate: 20/20 passed** (the 19 feature tests run in ~2s;
  P11's 77-test subprocess dominates the file's time).
* **Full suite: 161 passed** (141 prior + 20 new) in 2106s.
  Existing tests were not modified except version pins
  (below). CLI demo in an isolated home still produces the
  exact 19-event trail (gate I2, subprocess, real CLI);
  `demo --verify` produces 20 (the one verification event,
  outcome skipped) — smoke-verified by hand along with the
  export CLI in stdout and --out modes. Page JS passes
  `node --check`.

## Verifier context fix (post-build, pre-push)

A parent live probe against a real model (muse-spark-1.3)
found a systematic verifier blind spot. A task that recalled
the trusted memory "Ryan prefers email-only scoping: written
email exchange, never phone or video calls" produced a
deliverable correctly following it — and the verifier flagged
outcome "issues", claiming the email-only preference "appears
to be an invented fact rather than following actual stored
preferences". The verifier judged the deliverable against the
goal alone and never saw the memory context the planner
legitimately had, so ANY memory-grounded (or skill-hinted)
output would be misjudged as invented, every time.

The fix is confined to the verifier prompt construction in
`Agent._run_verification` (`agent.py`): the prompt gains a
labeled section, "Context the planner was given (trusted
memory and skills):", carrying the SAME context rendered into
the planner's prompts — the task's recalled trusted memory
texts and skill hints, re-resolved from the stored
`memory_context` / `skill_hints` ids via `_planning_context`,
so a record that has since left the trusted set drops out
there too — plus one instruction sentence: claims traceable
to that context count as grounded. The section is omitted
entirely when the task recalled no context, leaving that
prompt byte-identical to its pre-fix shape. Nothing else
about the verifier changed: same provider/profile, same
outcome vocabulary, the verdict still never changes task
status, usage still recorded.

Gate coverage: new case I8 in `tests/test_sanity_gate_v11.py`
(2 tests; the gate file is now 22 tests). With the stub
provider capturing prompts, a verify=True task that recalls a
trusted memory shows the trusted text inside the verifier
prompt under the heading; a candidate memory's marker text
appears in NEITHER the planner prompts nor the verifier
prompt; and a verify=True task with no recalled context
produces a verifier prompt with no context section.

Results on this (loaded) machine: the gate's feature tests
pass 21/21 in ~2.5s. In the full gate-file run, P11 (the
prior-gates subprocess) failed, and the full suite finished
160 passed / 3 failed — all three failures are prior-gates
subprocess wrappers (v11 P11, v8 K8, v9 K7) cascading from a
single pre-existing innermost assertion,
`tests/test_sanity_gate.py::test_k3`'s nanosecond check that
the trail record's `ts_ns` precedes the written file's
`mtime_ns`, which lost by ~0.1 ms under the deep subprocess
nesting and machine load of a full run. Re-run standalone,
the innermost file passes 4/4 and the exact innermost
combination (v1+v2) passes 9/9 three times in a row; the
original build's quiet-machine run (above) passed the suite
161/161. The failing assertion exercises the MockModel
write/approval flow and is untouched by this fix, which
changes only verifier prompt text. (A first full-suite
attempt for this fix was lost ~21 minutes in when the VM
rebooted and wiped /tmp; one F was visible at that point,
identity unrecoverable, consistent with the same flake.)

## Recorded pin changes

Version pins bumped `0.10.0` -> `0.11.0` in
`src/ghost_agent/__init__.py`, `pyproject.toml`, and the
three existing assertions that name the version:
`tests/test_server_v4.py` (1) and `tests/test_server_v5.py`
(2) — the same recorded pin update prior versions made. No
other existing test was touched.

## One gate fixture corrected during the build

I6 originally asserted the exported line for the fixture goal
"write exp.txt with: hello export" would contain both a
write_file and a read_file step. MockModel's plan for that
goal is a single write step (its read-step pattern needs the
word "read" in the goal), so the fixture expectation was
wrong, not the export. The assertion now pins the exact
executed-step list `[("write_file", "done")]` — stricter
than the original, and a fixture correction in the NEW gate
file only. No product assertion was loosened anywhere.

## Files changed

* New: `tests/test_sanity_gate_v11.py`, `BUILD_REPORT_V11.md`.
* Edited: `src/ghost_agent/providers.py` (tiers, preset
  tiers, effective-tier merge, "best" strategy name,
  system_block tier fields), `src/ghost_agent/models.py`
  (prompt context, adapter `complete()` methods, router
  context threading + `_best_pick` + `Router.complete`),
  `src/ghost_agent/agent.py` (recall selection/rendering,
  `create_task(verify=...)`, verifier pass),
  `src/ghost_agent/server.py` (`training_jsonl`,
  /api/export-training, POST verify, System heading + tier
  pill + training download, chat verify checkbox),
  `src/ghost_agent/cli.py` (`demo --verify`,
  `export-training`), `src/ghost_agent/__init__.py`,
  `pyproject.toml`, `README.md` ("Ghost Intelligence"
  section, "best" strategy bullet, test list, report link),
  and the three version pins above.

## Honest limits

* **No live provider was called.** All verification is
  stubbed at the `urllib.request.urlopen` seam with dummy
  keys, per the build constraints — the verifier and the
  "best" strategy are proven against scripted providers, not
  against real endpoints.
* Recall is keyword overlap, not semantic search: a trusted
  memory phrased with none of the goal's meaningful tokens
  will not be recalled, and a memory sharing one common token
  will be. The scoring is deliberately explainable from the
  record; ranking quality is the known trade.
* Skill hints match on the skill's name plus its source goal
  (the record's own `goal` field when one exists, else the
  goal of the task it was proposed from). A skill whose name
  and source goal share no token with a new goal will not
  hint, however related the work really is.
* The verifier judges with the same model that did the work
  (by design — one provider per task, no second opinion from
  a different brain), and in this version its verdict is
  evidence only: nothing retries, blocks, or escalates on an
  "issues" verdict yet.
* The training export is a dataset seed, not a training run:
  no fine-tune is performed, scheduled, or implied by it.
* The Android app (v1.5.0 parity) is a separate build and is
  not part of this report.
