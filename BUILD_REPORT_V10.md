# BUILD REPORT — Ghost Agent v0.10.0

Model profiles + router strategies. Python stdlib only, zero
dependencies, local-first, $0. Built and verified locally;
**not pushed** (the parent verifies and pushes).

## What shipped

* **Provider profiles (`src/ghost_agent/providers.py`, new).**
  `providers.json` in the agent home:
  `{strategy, default, chain, profiles:{name:{base_url, model,
  api_key_env, kind}}}`. The home file wins when present; on
  first run it is seeded with ten presets — Meta
  (`https://api.meta.ai/v1`, `muse-spark-1.3`, `META_API_KEY`),
  OpenAI (`gpt-4o-mini`), OpenRouter (`openai/gpt-4o-mini`),
  Fireworks (`accounts/fireworks/models/llama-v3p3-70b-instruct`),
  Groq (`llama-3.3-70b-versatile`), Together
  (`meta-llama/Llama-3.3-70B-Instruct-Turbo`), Gemini
  (`gemini-2.5-flash`), keyless Ollama (`llama3.1`) and LM
  Studio (`local-model`), and Anthropic (`claude-sonnet-4-5`,
  kind `anthropic`) — with `default: null` (nothing pinned, so
  legacy env behavior rules until a user edits the file) and
  `chain` listing all ten (chain skips unconfigured profiles,
  so the seed is safe). Loading is defensive in the
  `ecosystem.py` pattern: a malformed file yields a visible
  `load_error`, a `providers_load_failed` trail event, routing
  falls back to legacy env behavior, and the broken file is
  never overwritten. Keys are referenced by env-var NAME only —
  a key value is never written to `providers.json`, the store,
  or the trail. A profile is *configured* when `api_key_env`
  is null (keyless local) or that env var is set and non-empty.
* **Back-compat (hard requirement, kept).** With no `default`
  pinned, routing is exactly v0.9.0: the
  `GHOST_AGENT_API_KEY` / `GHOST_AGENT_BASE_URL` /
  `GHOST_AGENT_MODEL` env adapter (the implicit `env`
  profile) serves, MockModel falls back on a first-call
  failure only. `Router(prefer_real=...)` and `router.real`
  keep working as the existing gates use them; `plan()` keeps
  its legacy shape (now also carrying `provider`).
* **Anthropic adapter (`AnthropicModel` in `models.py`).**
  `POST {base_url}/v1/messages`, headers `x-api-key` +
  `anthropic-version: 2023-06-01` + JSON content type, body
  `{model, max_tokens: 2048, messages:[{role:"user",
  content:prompt}]}`; text blocks are concatenated; usage
  `{input_tokens, output_tokens}` normalizes into the existing
  `usage_record` shape (source `provider`). Same `next_step`
  contract, same legacy full-plan buffering, an empty content
  list is `ModelError("provider returned an empty response")`,
  same 90s timeout and error shapes.
* **Router strategies** (`providers.json` `strategy`):
  `default` (the named default profile serves; first-call
  MockModel fallback as today), `chain` (FIRST call only —
  chain profiles tried in order, unconfigured skipped,
  failures recorded as `name: reason`, first success owns the
  task for life and carries the hop record in
  `fallback_reason`; all-fail → MockModel with the combined
  reason; zero configured chain profiles → MockModel with an
  explicit "no configured profiles" reason, never a silent
  env substitution), `cheapest` (among configured profiles
  with KNOWN pricing, the lowest (input+output) per-million
  sum; unpriced-only → behaves as `default`). In ALL
  strategies a mid-task provider failure still fails the
  task — no brain switching (gate v7 K5 semantics preserved).
* **Per-task override + attribution.** `Agent.create_task`
  gains `profile=`; unknown or unconfigured names raise
  `ValueError` before any model call and create no task
  (`POST /api/tasks` surfaces 400; `ghost-agent demo
  --profile` exits 2 with the message). Task records gain
  `provider` (profile name / `env` / `mock`) alongside
  `model`; `model_usage` trail events carry the provider in
  their detail (usage dicts are copied before the field is
  added — adapter state is never mutated). `/api/analytics`
  usage gains an exact `by_provider` rollup from task records
  (pre-v0.10.0 records derive `mock`/`env` from their model).
  `/api/system` gains a `providers` block (strategy, default,
  chain, per-profile `{model, base_url, kind, api_key_env,
  configured, key_present}`) — presence booleans only.
  Scheduler-fired tasks use the strategy (no override).
* **Pricing.** Two additions only, Anthropic's published list
  prices (source comment in `models.py`): `claude-sonnet-4-5`
  $3.00/$15.00 and `claude-haiku-4-5` $1.00/$5.00 per million.
  Everything else unpriced stays cost `unknown`.
* **Dashboard (minimal page edits, same design language).**
  The System page renders the providers block (strategy /
  default / chain line, per-profile model + kind + configured
  and key-presence pills); the Chat page composer gains a
  profile picker populated from `/api/system` (empty choice =
  "strategy default", unconfigured profiles disabled, selection
  preserved across refreshes, creation errors surfaced in the
  chat status line); Analytics gains a tokens-by-provider bar
  row. Drawer unchanged — still exactly 12 pages; the page
  script passes `node --check`.
* **Keyless profiles.** An adapter built for a profile with
  `api_key_env: null` sends NO `Authorization` header at all;
  a configured-but-unset named env var fails honestly with
  `ModelError("<ENV> is not set")` — the same message the
  legacy adapter has always produced.

## Gate and suite results

* Gate written FIRST (`tests/test_sanity_gate_v10.py`, 19
  tests) and baselined on the v0.9.0 tree: all 17 feature
  tests failed for designed reasons (no `providers` module /
  no `profile` kwarg / no `provider` field / no `providers`
  payload); P10 (prior-gates subprocess) passes on old code by
  design and was excluded from the baseline.
* Gate cases: P1 seed + home-wins + malformed→legacy fallback;
  P2 default strategy reaches the named profile's URL; P3
  chain failover (task owned by the second profile, hop
  recorded), all-fail → MockModel with the combined reason,
  unconfigured chain entries skipped; P4 cheapest picks the
  mini-priced profile over the spark-priced one, unpriced-only
  set behaves as default; P5 override honored, unknown /
  unconfigured → creation error with zero tasks created, HTTP
  400 vs 201; P6 Anthropic request shape (path, `x-api-key`
  equal to the dummy env key, version header, body),
  text-block parsing, usage normalization (110/55/165 tokens,
  estimated cost $0.001155 at the new list prices), empty
  content list → the shared empty-response reason; P7 keyless
  Ollama attempted with no Authorization header; P8 mid-task
  chain failure fails the task with the provider unchanged and
  the second profile never called; P9 `/api/system` contains
  the configured profile's `key_present: true` and the dummy
  secret string nowhere in the payload; P9b exact
  `by_provider` analytics rollup; P10 prior gates v1–v9 in a
  subprocess (58 tests).
* **Gate: 19/19 passed** (579s including the P10 subprocess).
* **Full suite: 141 passed** (122 prior + 19 new) in 1181s.
  Existing tests were not modified except version pins
  (below). CLI demo in an isolated home still produces the
  exact 19-event trail; page JS passes `node --check`.

## Recorded pin changes

Version pins bumped `0.9.0` → `0.10.0` in
`src/ghost_agent/__init__.py`, `pyproject.toml`, and the three
existing assertions that name the version:
`tests/test_server_v4.py` (1) and `tests/test_server_v5.py`
(2) — the same recorded pin update prior versions made. No
other existing test was touched.

## Files changed

* New: `src/ghost_agent/providers.py`,
  `tests/test_sanity_gate_v10.py`, `BUILD_REPORT_V10.md`.
* Edited: `src/ghost_agent/models.py` (Anthropic adapter,
  per-profile adapters + strategies in the Router, two price
  rows), `src/ghost_agent/agent.py` (providers wiring,
  `create_task(profile=...)`, provider passed to the loop),
  `src/ghost_agent/server.py` (`/api/system` providers block,
  `by_provider` rollup, POST profile + 400, System providers
  section, chat profile picker), `src/ghost_agent/cli.py`
  (`demo --profile`), `src/ghost_agent/__init__.py`,
  `pyproject.toml`, `README.md` ("Models and routers" section,
  test list, report link), and the three version pins above.

## Honest limits

* **No live provider was called.** All verification is stubbed
  at the `urllib.request.urlopen` seam with dummy keys, per
  the build constraints — the Anthropic adapter is verified
  against the documented Messages API shape, not against
  api.anthropic.com. A live smoke against a keyless local
  Ollama/LM Studio (or a real key) is a follow-up the parent
  can run.
* The seeded presets' model ids are the providers' current
  public defaults as of Nov 2026; a provider retiring a model
  id makes that profile fail honestly at call time (recorded
  fallback / task failure), never silently.
* `cheapest` compares only the two models with known pricing
  among configured profiles; it is a list-price comparison,
  not a quality or latency judgment.
* Strategy resolution happens at a task's first call in the
  process that creates it; editing `providers.json` mid-flight
  does not retarget running tasks (by design — a task's brain
  and provider are fixed for its life).
* The Android app (v1.5.0 parity) is a separate build and is
  not part of this report.
