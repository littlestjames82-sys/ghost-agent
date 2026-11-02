# Ghost Agent v0.8.0 — Build Report

## Sanity Gate v8 output (gate first, per standing rule)

Gate file: `tests/test_sanity_gate_v8.py` (8 tests: K1–K7 plus K8
back-compat). Written FIRST and baselined against the v0.7.0 code
before any implementation:

Baseline on v0.7.0 code (recorded before implementation):

```
K1 seed                      FAILED — ModuleNotFoundError: ghost_agent.ecosystem
K2 list_projects             FAILED — ModuleNotFoundError
K3 repo_status               FAILED — ModuleNotFoundError
K4 read_project_file         FAILED — ModuleNotFoundError
K5 governance + MockModel    FAILED — the three tools classify deny (unknown)
K6 HTTP + page + import      FAILED — GET /api/ecosystem is 404
K7 robustness                FAILED — ModuleNotFoundError
(K8 back-compat was not part of the baseline run; it re-runs the
 prior gates against the NEW code by design.)
```

Gate on v0.8.0 code:

```
K1–K7 (run directly):  7 passed in 1.45s
Whole gate file (incl. K8's subprocess re-run of gates
v1..v7b, 43 tests):    8 passed in 145.32s
```

Full suite (107 prior + 8 new gate tests = 115):

```
115 passed in 298.66s
```

What each gate case proves:

- **K1** — a fresh home seeds `ecosystem.json` from the bundled
  resource: 72 entries on disk and loaded, ghost-kernel in layer
  `kernel`, ghost-agent in `products`, no load error, nothing
  skipped; the bundled resource parses as a 72-entry list.
- **K2** — `list_projects` against a 3-project fixture registry
  (which also proves the loader prefers the home file over the
  bundle): all entries returned in order, the `kernel` layer
  filter returns exactly the kernel entry, an empty layer returns
  none, and `has_local` is computed live (real dirs true, missing
  path false).
- **K3** — `repo_status` against a real fixture git repo reports
  the repo's actual branch (cross-checked with git itself), a
  dirty count of exactly 1 after one file is modified, and the
  real last-commit subject; a plain directory answers "not a git
  repository"; a missing path answers "no local tree on this
  machine" with the repo slug in the output; an unknown project
  errors and names the registry as the fix.
- **K4** — `read_project_file` returns a fixture file's exact
  bytes; `../` traversal and an absolute path outside the root
  are both refused; an unregistered project is refused with the
  registry pointer; a 250,000-char file returns truncated with
  the marker, at the 200KB cap.
- **K5** — all three tools classify `read` (auto-allow) in
  governance; MockModel tasks "status of alpha-proj" and "list
  projects" plan the right tool and run to done with **zero
  approvals ever created**; "status of nope-proj" (unregistered)
  falls through to the old echo behavior instead of planning a
  doomed repo_status step.
- **K6** — `GET /api/ecosystem` is 200 with the fixture entries
  and matching `has_local` flags; the served page carries the
  Ecosystem section, sidebar nav, and a drawer listing each of
  the 12 pages exactly once, with Ecosystem grouped under
  INTELLIGENCE; the served page script passes `node --check`.
  Plus the import pin: a snapshot whose `ecosystem` key names a
  hostile project imports successfully and changes **nothing** —
  endpoint still shows the fixture registry and the home
  `ecosystem.json` is byte-identical afterwards.
- **K7** — a home `ecosystem.json` containing invalid JSON never
  crashes startup: the registry loads empty, `load_error` carries
  the reason (and a best-effort `ecosystem_load_failed` trail
  event is written), and the user's broken file is **not**
  overwritten. Entries missing `name`/`layer` and non-object
  entries are skipped (counted) while valid entries load. The
  server initializes and serves `/api/ecosystem` (200, count 0,
  load_error present) with the broken registry in place.
- **K8** — all 43 prior known-answer gate tests (v1–v7b) pass in
  the same run under the new code; nothing regressed underneath.

## What v0.8.0 is

**The studio's repo ecosystem, integrated as data + read-only
tools + a page.** Three pieces:

1. **The registry** (`src/ghost_agent/ecosystem.py`, new). A JSON
   list of `{name, repo, local_path, layer, role, source}`. The
   bundled seed (`src/ghost_agent/ecosystem_seed.json`, a
   byte-identical copy of the prep registry at
   `~/workspace/ghost-agent-ecosystem/registry-seed.json`: 72
   entries — kernel 1, law 12, language 1, rails 3, hosting 1,
   bodies 3, products 10, tools 36, other 5) ships as a package
   resource. The home's `ecosystem.json` is the source of truth
   when present; on first run the seed is copied there so the
   user owns an editable file. Loading is defensive (see K7).
   One instance per store, shared by the agent, the tools, and
   the dashboard (`get_ecosystem`). Seeding deliberately writes
   **no** trail event — a fresh home's trail belongs to the user
   (the CLI demo's trail is unchanged at 19 events); load
   *failures* do write best-effort trail events.
2. **Three read-class tools** (`tools.py`). `list_projects{layer?}`,
   `repo_status{project}`, `read_project_file{project, path}` —
   all classified `read` in governance, so they auto-allow under
   the same rule as every read. The registry is their allowlist:
   unknown projects are refused with a pointer to the registry,
   and `read_project_file` resolves its path and requires it to
   stay under the registered root (traversal raises `ToolError`,
   the sandbox's own refusal style), caps output at 200,000 chars
   with a truncation marker, and errors clearly on binary files.
   `repo_status` runs only read-only git commands
   (`rev-parse`, `status --porcelain`, `log -1`) with a 10s
   timeout and phrases every outcome honestly: branch + dirty
   count + last commit, or "not a git repository", or "no local
   tree on this machine" plus the repo slug. Both planner prompts
   now teach the three tools' exact signatures
   (`TOOL_SIGNATURES` / `TOOL_NAMES` in `models.py`). MockModel
   gained two deterministic patterns — "list projects" /
   "ecosystem" → `list_projects`; "status of <name>" →
   `repo_status` for registered names only (the Agent wires the
   registry into `Router.mock`; unregistered names fall through).
3. **The Ecosystem page** (`server.py`). The 12th page:
   sidebar nav + App Drawer entries under Intelligence, the
   command palette, and a section rendering shelves grouped by
   layer in stack order (kernel → law → language → rails →
   hosting → bodies → products → tools → other; unknown layers
   appended alphabetically). Each project card shows the role,
   the repo as a pill, the local path, a `local tree` /
   `no local tree` badge, and a provenance strip
   Source → Layer → Status built only from real fields (the
   seed's long source strings are display-truncated at 90 chars).
   A header line names the registry file and count and whether
   it was seeded; a red shelf surfaces `load_error` when the
   registry is broken. Backed by `GET /api/ecosystem`
   (`ecosystem_payload`), with `has_local` computed server-side
   per request. `GET /api/export` gains an `ecosystem` key with
   the raw registry; `POST /api/import` ignores that key (pinned
   in K6) — the registry is machine-local config, not portable
   user data.

Versions: `__version__` and `pyproject.toml` both 0.8.0;
`/api/system` reports 0.8.0; pyproject also declares the seed
JSON as package data so an installed wheel carries it.

## Verification beyond the gate

- **CLI demo, isolated home** (`GHOST_AGENT_HOME=/tmp/ga-v8demo`):
  exit 0; `demo_output.txt` byte-identical; same candidate skill;
  trail exactly **19 events** (unchanged from v0.7.0 — seeding is
  silent by design); the home gained a seeded `ecosystem.json`
  with 72 entries.
- **Live HTTP smoke** (fresh home, real registry, MockModel):
  `/api/system` = 0.8.0; `/api/ecosystem` = 72 entries, seeded,
  no load error, ghost-kernel `has_local: true`,
  ghost-bridge-queue `has_local: false` (it has no local tree in
  the seed); a "status of ghost-kernel" task ran to done —
  answered *"local tree at /home/hatch/workspace/ghost is not a
  git repository"*, verified truthful by hand (that tree has no
  `.git`; it is an API-synced build tree); a "list projects"
  task completed (see the output-cap note below); the served
  page contains the Ecosystem section; `/api/export` carries
  72 ecosystem entries.
- **Live git-backed spot checks** against the real registry:
  ghostcut → `branch main - 256 uncommitted change(s) - last
  commit cf5e79e9 (2026-05-17) docs: readme`; ghostchat →
  branch develop; storefront → branch master — real branches,
  counts, and commits read from the actual trees.
  `read_project_file` on the ghost-agent project returned this
  repo's own pyproject with `version = "0.8.0"`.

## Recorded changes to existing tests (with reasons)

- `tests/test_server_v4.py` (1 line) and `tests/test_server_v5.py`
  (2 lines): version pins `"0.7.0"` → `"0.8.0"`. Reason: the
  `/api/system` and export version assertions must name the
  current version — the same recorded pin update v0.6.0 and
  v0.7.0 made.
- No other existing test was modified. All 107 prior tests pass
  unmodified — including gate v5's drawer test (it pins each of
  its 11 pages appearing exactly once; a 12th page does not
  disturb that) and the v7 gates' prompt assertions (the new
  tool signatures are appended, the existing text is untouched).

## Honest limits

- **No GitHub API, no network — deliberate.** The runtime has no
  credential story for GitHub, so v0.8.0 reads local git trees
  only. The seed's 3 remote-only projects report their repo slug
  plus "no local tree on this machine"; CI, issues, and PR state
  are invisible to the agent in this version.
- **repo_status tells local truth, which is sometimes "not a
  git repository."** Several studio trees (ghost-kernel's build
  tree, ghostbus, ghost-agent itself) are API-synced working
  copies without `.git`. The tool reports that exactly; branch
  and commit data exist only for real clones. The registry data
  is not wrong — a local path was never a promise of a checkout.
- **`has_local` is a directory-existence check** at the
  registered path, computed live. It does not verify the tree is
  the right checkout, only that something is there.
- **Task records cap step output at 2,000 chars** (pre-existing
  loop behavior), so a `list_projects` step's stored output
  shows roughly the first 15 of 72 projects in the task detail.
  The tool itself returns the full registry (K2), and the
  Ecosystem page renders all of it; only the per-step record in
  the task view is capped, like any long read.
- **read_project_file reads the whole file into memory** before
  applying the 200KB output cap. Fine for the text files this
  tool exists for; a pathological multi-GB "text" file would be
  read before it could be refused. The binary check runs on the
  first 8KB, matching `search_files` practice.
- **The seed's known conflicts travel with it.** The prep notes
  (PREP_NOTES.md next to the source seed) record 8 conflicts and
  5 judgment calls in the source data — the GhostGuard family
  counts 10 repos in the audit but 11 on a roadmap board,
  ghost-agent is absent from the Nov 1 audit, three local
  projects have no repo anywhere. The build changed none of it:
  the registry is data, the home copy is the user's to correct,
  and the seed file here is byte-identical to the prep output.
- **MockModel's ecosystem patterns are substring heuristics**,
  like all its patterns: any goal containing "ecosystem" routes
  to `list_projects`. Deterministic and documented, not smart.
- **Suite runtime grew** to ~5 minutes: the gates nest
  subprocess back-compat runs two levels deep now (v8's K8 runs
  v7, whose K1 runs v1–v6). The cost of the standing
  gate-discipline, paid in wall-clock, not correctness.

## Files changed

- `src/ghost_agent/ecosystem.py` — new: registry loader
  (seed-on-first-run, defensive parse, validation, per-store
  cache), `LAYER_ORDER`, entry queries with live `has_local`.
- `src/ghost_agent/ecosystem_seed.json` — new: the bundled
  72-project registry (byte-identical to the prep seed).
- `src/ghost_agent/tools.py` — the three ecosystem tools
  (registry allowlist, git status with timeout, capped
  project-file reads), `TOOL_KINDS` entries, optional
  `ecosystem` constructor wiring.
- `src/ghost_agent/models.py` — `TOOL_SIGNATURES` / `TOOL_NAMES`
  extended; the legacy planner prompt now uses `TOOL_NAMES`;
  MockModel ecosystem attribute + two deterministic patterns.
- `src/ghost_agent/agent.py` — constructs the shared registry,
  passes it to Tools, wires it into the router's MockModel.
- `src/ghost_agent/server.py` — `ecosystem_payload`,
  `GET /api/ecosystem`, the export `ecosystem` key, the import
  docstring's ignore rule, and the Ecosystem page (nav, drawer,
  palette, section, `renderEcosystem`, refresh wiring, tool
  glyphs); module docstring updated.
- `src/ghost_agent/__init__.py`, `pyproject.toml` — 0.8.0;
  pyproject declares the seed as package data.
- `tests/test_sanity_gate_v8.py` — new gate (8 tests).
- `tests/test_server_v4.py`, `tests/test_server_v5.py` — version
  pins (recorded above).
- `README.md` — "Ecosystem registry (v0.8.0)" section (registry
  file, tools, page, read-only scope, import rule), plus
  governance/tools/state/Models/Honest-limits/Tests updates.

No push, no publish — per the brief.
