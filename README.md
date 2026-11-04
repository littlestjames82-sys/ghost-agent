![Ghost Agent](assets/ghost-agent-icon.jpg)

# Ghost Agent
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Release](https://img.shields.io/github/v/release/littlestjames82-sys/ghost-agent)](https://github.com/littlestjames82-sys/ghost-agent/releases) [![CI](https://github.com/littlestjames82-sys/ghost-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/littlestjames82-sys/ghost-agent/actions/workflows/ci.yml)

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

# Dashboard (sidebar UI: overview, board, approvals, tasks, chat,
# memory, skills, analytics, logs, ecosystem, schedules, system):
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
append-only `trail.jsonl`, a sandboxed `workspace/` directory that
is the only place write tools may touch, and `ecosystem.json` —
the project registry (seeded on first run; yours to edit).

## How it works

* **Store** — JSON/JSONL store. Stable ids, timestamps, and memory
  provenance (source, verified flag, superseded_by).
* **Governance** — every tool call is classified: `read_file`,
  `list_dir`, `echo_note`, `search_files`, and the v0.8.0 ecosystem
  tools (`list_projects`, `repo_status`, `read_project_file`)
  auto-allow; `write_file`, `append_file`, and `shell` require
  human approval; unknown tools are denied by default. Deny marks
  the step failed and stops the run honestly.
* **Tools** — sandboxed to the workspace root: `read_file`, `list_dir`,
  `echo_note`, `search_files` (read — literal substring search with
  file paths + line numbers, capped at 50 matches, binary and >1 MB
  files skipped), `write_file`, `append_file` (write — raw append,
  approval-gated exactly like a write), `shell` (exec, timed out
  and exit-code captured). Paths escaping the sandbox are refused.
  The ecosystem tools are sandboxed differently: the registry is
  their allowlist, and `read_project_file` never leaves a
  registered project's local root (see "Ecosystem registry
  (v0.8.0)" below).
* **Agent loop** — since v0.7.0 the loop is **plan-act-observe**:
  the model is asked for one step at a time, with the completed
  steps and their actual outputs in context, so a step can be
  planned from what earlier steps really found. Per step:
  governance check, approval if needed, **record trail**, execute,
  verify (file exists / exact content / exit 0), then ask again —
  until the model answers `done`. See "Plan-act-observe loop
  (v0.7.0)" below for the guards (step cap, loop detection,
  validation feedback, fallback honesty). A fully successful run
  proposes a candidate skill (name, steps, tools, permissions)
  that stays **untrusted** until a human promotes it.
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

## Plan-act-observe loop (v0.7.0)

* **One step at a time, with the evidence in view.** v0.6.0 planned
  a whole task in a single model call, so a write step's content
  was composed *before* its read steps ran — summaries came out
  generic and ungrounded. Now the model is asked for the NEXT step
  given the goal plus the history: every completed step with its
  tool, args, status, and observed output (each output truncated
  to 1,000 chars; the history is capped at ~8,000 chars, oldest
  dropped first). It answers one step object — or `{"tool":
  "done"}`. The task's step list grows as the loop runs, and every
  next-step request writes a `replan` trail event with its
  iteration number, so the loop is fully auditable in Logs.
  Providers that still answer with a full plan array are served
  stepwise from that array, with usage recorded once, when it was
  fetched.
* **Per-call usage.** Every model call in the loop records its own
  `model_usage` event and accumulates into the task's totals, so
  `calls` now exceeds 1 for real tasks — that is the loop working,
  not double-counting.
* **Guards.** At most **12 executed steps** per task ("step cap
  reached"). If the model proposes the identical step (tool +
  args) as the last executed step twice in a row, the task fails
  with "loop detected" instead of burning calls. Governance is
  unchanged and per step: a write/exec proposal is recorded and
  pauses the task in `waiting_approval` *before* executing, no
  model call happens while a task waits, and approval resumes the
  loop exactly where it stopped.
* **Validation feedback.** Both planner prompts now teach each
  tool's **exact argument signature** (`search_files{query,
  path?}`, `write_file{path, content}`, …). And if a proposed step
  still arrives with unusable args, the loop no longer dies on it
  (the v0.6.0 gauntlet lost 3 of 10 goals to exactly that): the
  step is parked as `rejected`, the validation error is fed back
  in the history, and the model gets to propose a corrected step —
  at most **2 correction retries per task**, after which the task
  fails honestly with the validation error in the trail.
* **Fallback honesty.** Only a task's FIRST model call may fall
  back to MockModel (the reason is recorded, as always). If a
  later call fails, the task **fails** with the provider's reason
  in the trail — the brain never silently changes mid-task, and
  the task's `model` field always names the brain in charge.
  MockModel serves its deterministic plans through the same
  next-step interface, one step at a time, so its tasks behave
  exactly as before.
* **muse-spark pricing.** The built-in price table now includes
  the muse-spark family at Meta's published $1.25 input / $4.25
  output per million tokens, so Muse Spark runs report an
  estimated cost instead of "unknown".

## Ecosystem registry (v0.8.0)

Ghost Agent knows the studio's own projects through a **registry**:
a JSON list of `{name, repo, local_path, layer, role, source}`
entries, where `layer` is the stack layer (kernel, law, language,
rails, hosting, bodies, products, tools, other).

* **The file.** `GHOST_AGENT_HOME/ecosystem.json` is the source of
  truth. On first run it is seeded from the bundled registry
  (72 projects) and written to the home — from then on it is yours
  to edit: add a project, fix a path, change a layer. Loading is
  defensive: a registry that cannot be parsed, or entries missing
  `name`/`layer`, never crash startup; the problem is reported on
  the Ecosystem page and in the trail, invalid entries are
  skipped, and a broken file is never overwritten by the agent.
* **The tools** (all read class — they auto-allow like every read):
  `list_projects{layer?}` lists the registry with each project's
  local-tree presence computed live; `repo_status{project}` reports
  a registered project's branch, uncommitted-change count, and
  last commit from its local git tree (or, honestly, that there is
  no local tree on this machine, or that the tree is not a git
  repository); `read_project_file{project, path}` reads a text file
  inside a registered project's root — the resolved path must stay
  under the root, output is capped at 200KB with a truncation
  marker, and binary files get a clear error. Unknown project
  names are refused with a pointer to the registry.
* **The page.** The Ecosystem page (the 12th, under Intelligence)
  renders the registry as shelves by stack layer — kernel first —
  with a card per project: role, repo, local path, a local-tree
  badge, and a Source → Layer → Status provenance strip. It is
  backed by `GET /api/ecosystem`.
* **Scope, honestly.** This pass is **read-only and local**:
  status comes from local git trees only — there are **no network
  calls and no GitHub API** (a project with no local tree here
  reports exactly that, plus its repo slug). Writes stay confined
  to the agent's own workspace, exactly as before; the ecosystem
  tools can look at the studio's projects but never touch them.
* **Export/import.** `GET /api/export` carries the registry under
  an `ecosystem` key for visibility. `POST /api/import` **ignores**
  that key: the registry is machine-local config (its paths name
  this machine's trees), not portable user data, so importing a
  snapshot never replaces the local registry.

MockModel also learned two deterministic patterns: a goal
mentioning "list projects" or "ecosystem" plans a `list_projects`
step, and "status of <project>" plans a `repo_status` step — only
for a name the registry actually holds (anything else falls
through to the older patterns).

## Token budget, tree facts, fuller records (v0.9.0)

Three fixes from the Nov 2 live gauntlet and ecosystem probes:

* **A per-task token budget.** The loop re-sends its history on
  every call, so the gauntlet's document-heavy goals cost
  35k–45k tokens each — and nothing stopped a run from spending
  on. `GHOST_AGENT_TOKEN_BUDGET` now caps a task's total recorded
  provider tokens: an integer, **default 100000**, with **0 or a
  negative value disabling** the guardrail. Each task record
  carries its `token_budget` (the cap, or null when disabled),
  and `GET /api/system` reports the configured value. When a call
  brings the total to the budget and the task still has work, it
  stops *before* any further model call or tool execution:
  status `failed`, error `token budget reached (<used> of
  <budget> tokens)`, a `budget_exceeded` trail event carrying the
  used/budget pair, and no skill proposal. A call that answers
  "done" is not cut off — the work finished. MockModel records
  zero usage and therefore never trips the budget: it guards
  provider spend, and only provider spend.
* **`repo_status` reports tree facts for non-git trees.** Most
  studio trees are API-synced copies without `.git`, so
  "not a git repository" was all v0.8.0 could say about them.
  Now that answer also carries the tree's recursive **file
  count** (the scan stops at 10,000 files and reports `10000+`),
  the **newest file modification time** in ISO form, and a
  `carries:` list of the usual root markers found — any
  `README*`, `BUILD_REPORT.md`, `pyproject.toml`,
  `package.json` — or `carries: none of the usual markers`. The
  same fallback applies when the git commands themselves fail.
  The git-repo answer (branch, dirty count, last commit) is
  unchanged.
* **Step outputs are stored fuller.** A task record used to keep
  only the first 2,000 chars of a step's output, so a 72-project
  `list_projects` result showed roughly 15 lines in the task
  detail. The stored cap is now **8,000 chars**. (What the model
  sees — the loop's history budget of 4,000 chars per output and
  40,000 total — is a different budget and is unchanged.)

## Models

* **MockModel (default in tests/demo)** — a deterministic, rule-based
  planner that pattern-matches a small set of phrasings
  ("write X with: …", "read X", "run command: …", "demo", and since
  v0.6.0 "find/search …", "append … to …", "summarize …"; since
  v0.8.0 also "list projects"/"ecosystem" and "status of
  <project>"). It is **not intelligent**; it exists so the loop,
  governance, and trail can be exercised and tested without any
  provider.
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
  provider fails on a task's FIRST planning call, the router falls
  back to MockModel for that whole task and records the fallback
  reason in the trail; a failure on any later call fails the task
  instead (see the v0.7.0 loop section above).

## Models and routers (v0.10.0)

* **Provider profiles** — `providers.json` in the agent home
  names the providers the agent may use:

  ```json
  {"strategy": "default", "default": null, "chain": ["meta", "openai"],
   "profiles": {"meta": {"base_url": "https://api.meta.ai/v1",
                         "model": "muse-spark-1.3",
                         "api_key_env": "META_API_KEY",
                         "kind": "openai-compatible"}}}
  ```

  On first run the file is seeded with ten presets — Meta
  (`muse-spark-1.3`), OpenAI (`gpt-4o-mini`), OpenRouter,
  Fireworks, Groq, Together, Gemini (all OpenAI-compatible chat
  endpoints), keyless local Ollama and LM Studio, and Anthropic
  (`claude-sonnet-4-5`, `kind: "anthropic"`). The home file
  wins when present; a malformed file is never overwritten —
  loading reports a visible `load_error`, records a trail
  event, and routing falls back to the legacy env behavior.
* **Keys are referenced by env-var NAME only.** A key value is
  never written to `providers.json`, the store, or the trail;
  each adapter reads its named env var at call time. A profile
  is *configured* when its `api_key_env` is null (keyless
  local) or that env var is set and non-empty. `/api/system`
  exposes the profiles with `configured` / `key_present`
  booleans only — key values never appear there either (the
  System page renders them with presence pills).
* **Back-compat.** With no `default` pinned in `providers.json`
  (the seeded state), behavior is exactly the pre-v0.10.0
  legacy path: the `GHOST_AGENT_API_KEY` /
  `GHOST_AGENT_BASE_URL` / `GHOST_AGENT_MODEL` env adapter
  serves as an implicit `env` profile, with MockModel fallback
  on a first-call failure.
* **Strategies** (`strategy` in `providers.json`):
  * `default` — the profile named in `default` serves; on a
    first-call failure the task falls back to MockModel with
    the reason recorded.
  * `chain` — at the task's FIRST call only, the profiles
    named in `chain` are tried in order (unconfigured ones
    skipped); each failure is recorded, the first success
    owns the task for life, and if every configured chain
    profile fails the task falls back to MockModel with a
    combined reason naming the failures.
  * `cheapest` — among configured profiles whose model has
    known pricing in the built-in table, the lowest (input +
    output) per-million sum serves; if no configured profile
    is priced, it behaves as `default`.
  * `best` (v0.11.0) — among configured profiles, the highest
    *tier* serves (see Ghost Intelligence below); if none are
    configured, it behaves as `default`.

  In every strategy, a mid-task provider failure still fails
  the task — brains never switch mid-task — and
  `fallback_reason` records what actually happened.
* **Per-task override and attribution** — `create_task` /
  `POST /api/tasks` accept an optional `profile` (the Chat
  page has a picker; `ghost-agent demo --profile NAME` is the
  CLI path). An unknown or unconfigured name is an honest
  creation error (HTTP 400), never a silent substitution.
  Every task record names its owner in `provider` (a profile
  name, `env`, or `mock`) alongside `model`; `model_usage`
  trail events carry the provider, and `/api/analytics` rolls
  usage up `by_provider` exactly (tasks are
  single-provider). Scheduler-fired tasks use the strategy —
  no override.
* **Anthropic** — profiles with `kind: "anthropic"` speak the
  Messages API: `POST {base_url}/v1/messages` with `x-api-key`
  and `anthropic-version: 2023-06-01` headers, `max_tokens`
  2048, text blocks concatenated, and `input_tokens` /
  `output_tokens` normalized into the same usage records.
  Only two Anthropic list prices are in the cost table —
  `claude-sonnet-4-5` $3.00/$15.00 and `claude-haiku-4-5`
  $1.00/$5.00 per million (Anthropic's published list
  prices); every other unpriced model stays cost `unknown`,
  as before.

## Ghost Intelligence (v0.11.0)

Ghost Intelligence is the name for this runtime's brain: the
router, plus what the router's planner gets to know and how its
work is checked. It has three levels, by design:

* **Level 1 — shipped.** The multi-model router (v0.10.0:
  profiles, strategies, per-task override, attribution), plus
  the three additions below.
* **Level 2 — the on-ramp is shipped; training is later.** The
  training-data export (below) turns governed runs into a
  curated dataset FIRST, because a dataset is the asset every
  later step stands on. That is the Ghost Hands precedent: its
  on-device planner was trained from a curated dataset of
  examples (718 of them), not by pointing a trainer at raw
  logs. When a fine-tune happens, it trains on exports like
  this one — successful, human-approved runs with their
  approval decisions attached.
* **Level 3 — parked by decision.** A foundation model of our
  own is a long-term research direction, not a roadmap item;
  nothing in this build pretends otherwise.

The v0.11.0 additions:

* **Memory-informed planning.** When a task is created, the
  agent selects TRUSTED memories whose text shares keyword
  overlap with the goal (tokenized on non-alphanumerics,
  stopwords and short tokens dropped, scored by distinct
  shared tokens; top 5). Their texts ride in EVERY planner
  prompt for the task under "Relevant memory (trusted,
  user-approved):". The chosen ids are stored on the task
  (`memory_context`) and one `memory_recalled` trail event
  records the recall. CANDIDATE memories never reach a
  prompt — untrusted text does not steer the planner.
* **Skill hints.** The same pass selects TRUSTED skills (top
  2) whose name or source goal overlaps the goal, and the
  prompt gains "Relevant skill '<name>' (trusted): a previous
  successful run used these tools in order: …". It is a hint,
  not a script — the planner may deviate. Candidate skills
  never hint. Ids ride on the task (`skill_hints`) with one
  `skill_hinted` trail event.
* **Verifier pass (opt-in).** `create_task(verify=True)`,
  `POST /api/tasks {"verify": true}`, `ghost-agent demo
  --verify`, or the Chat page's "Verify result" checkbox. When
  such a task reaches done, the SAME provider that ran it
  makes ONE extra call judging the deliverable — the files
  its write steps produced, read back from the workspace (or
  the step outputs when it wrote none) — against the goal,
  demanding a strict JSON verdict. The record lands on the
  task (`verification`) and in the trail: `grounded`,
  `issues` (with the issues list verbatim), `unparseable`,
  or `error`. A MockModel-owned task records
  `{"outcome": "skipped", "reason": "mock model"}` and makes
  no call. The verdict NEVER changes the task's status — it
  is evidence, not a gate — and its token usage is recorded
  like any other call (with `over_budget: true` noted if that
  usage carries the task past its token budget after the
  work was already done).
* **Tier-aware routing.** Profiles carry an optional `tier`:
  `frontier`, `standard`, or `small` (presets: Meta and
  Anthropic frontier; OpenAI, OpenRouter, Fireworks, Groq,
  Together, Gemini standard; Ollama and LM Studio small). A
  profile's *effective* tier is its own valid tier, else the
  preset tier for its name, else none — so a `providers.json`
  written before tiers existed still ranks correctly, and
  the file is never rewritten to add them. An invalid tier
  value is ignored for selection but shown raw in
  `/api/system` (`tier_configured`, beside the effective
  `tier`). The new `best` strategy serves the highest-tier
  configured profile (ties break by the file's profile
  order); an unknown strategy string behaves as `default`.
* **Training-data export.** `GET /api/export-training`
  (add `?include_failed=1` for failed runs) or
  `ghost-agent export-training [--out PATH]
  [--include-failed]` emits JSONL: one line per completed
  task run with its goal, provider, model, status, the
  executed steps (output truncated to 2000 chars), the
  human approval decisions recovered from the trail, and
  usage totals. The CLI also prints a data card (counts by
  status and provider, total tokens). The export is built
  from task records and the trail only — no providers
  config, no memory or skill records, no key values.

The dashboard's System page heads this whole block "Ghost
Intelligence — models & routing", with each profile's tier
shown as a pill.

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
* Ecosystem status is local-only: `repo_status` reads local git
  trees and nothing else. There is no GitHub API in v0.8.0 (the
  runtime has no credential story for it), remote-only state
  (CI, issues, PRs) is invisible to it, and registry `local_path`
  values name this machine's trees — a registry copied to another
  machine truthfully reports "no local tree" until its paths are
  edited there.
* No fine-tuned or foundation model in v1 (out of scope by design).

## Tests

```sh
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate.py -v      # v1 known-answer gate first
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v2.py -v   # v2 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v3.py -v   # v3 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v4.py -v   # v4 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v5.py -v   # v5 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v6.py -v   # v6 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v7.py -v   # v7 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v7b.py -v  # v7b live-fix gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v8.py -v   # v8 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v9.py -v   # v9 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v10.py -v  # v10 known-answer gate
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate_v11.py -v  # v11 known-answer gate
PYTHONPATH=src python3 -m pytest tests/ -q                         # full suite
```

See [BUILD_REPORT.md](BUILD_REPORT.md) (v1),
[BUILD_REPORT_V2.md](BUILD_REPORT_V2.md) (v2),
[BUILD_REPORT_V3.md](BUILD_REPORT_V3.md) (v3),
[BUILD_REPORT_V4.md](BUILD_REPORT_V4.md) (v4),
[BUILD_REPORT_V5.md](BUILD_REPORT_V5.md) (v5),
[BUILD_REPORT_V6.md](BUILD_REPORT_V6.md) (v6),
[BUILD_REPORT_V7.md](BUILD_REPORT_V7.md) (v7),
[BUILD_REPORT_V8.md](BUILD_REPORT_V8.md) (v8),
[BUILD_REPORT_V9.md](BUILD_REPORT_V9.md) (v9),
[BUILD_REPORT_V10.md](BUILD_REPORT_V10.md) (v10), and
[BUILD_REPORT_V11.md](BUILD_REPORT_V11.md) (v11) for gate results
and counts.

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

---
<!-- ghost-studio-footer -->
## More from Ghost Developer Studio

Ghost Developer Studio builds developer tools, AI-agent safety software, and practical apps, and sells by showing working software, not slide decks.

- [Roadmap board](https://github.com/littlestjames82-sys/ghost-roadmaps): where every studio product stands
- [Agent Seatbelt](https://github.com/littlestjames82-sys/agent-seatbelt): deterministic guardrails for AI coding agents
- [Ghost Hands](https://github.com/littlestjames82-sys/ghost-hands): governed, recorded, replayable agent hands
- [GhostSlice](https://github.com/littlestjames82-sys/ghostslice): agent context and prompt optimizer
- [GhostIDE](https://github.com/littlestjames82-sys/ghostide-android): fully offline AI code editor for Android
- [All studio projects](https://github.com/littlestjames82-sys?tab=repositories)
