# Ghost Agent v0.5.0 - Build Report (Trust Ladder reskin + brand icon)

## Sanity Gate v5 - FIRST, as required

`tests/test_sanity_gate_v5.py`, 5 known answers, run before any
reskin code was written, then re-run from scratch on the finished
build.

| Case | Known answer | Final |
| --- | --- | --- |
| K1 Back-compat | v1+v2+v3+v4 gates (20 tests) pass in the same run (subprocess) | PASS |
| K2 Shelf semantics | Served page carries the Memory Trust Ladder (dashed-amber candidates shelf, solid-green trusted shelf, distinguishing CSS rules present) and the Board renders its columns as shelves colored per state | PASS |
| K3 Drawer completeness | Drawer markup lists each of the 11 pages exactly once (data-page), grouped OPERATE / INTELLIGENCE / SYSTEM in order | PASS |
| K4 Bottom nav | Bottom-nav element with 4 targets (overview / board / chat / drawer) and the <=820px media query shows it and hides the sidebar | PASS |
| K5 No behavior drift | Seeded /api/board, /api/overview, /api/approvals equal the payload functions' outputs; token gate still 401 without / 200 with | PASS |

**Final gate result: 5/5 PASS** (full from-scratch run on the final
code, icon route included).

### Honest gate history

- Pre-implementation run (against v0.4.0, gate written first):
  **2/5** - K1 and K5 passed; K2, K3, K4 failed because the shelves,
  drawer, and bottom nav did not exist yet. That is the baseline the
  reskin had to move.
- Post-implementation run 1: **4/5** - K2's assertion looked for the
  literal `col shelf shelf-` in the served page, but renderBoard
  builds the class by concatenation from the colShelf map, so that
  literal never appears in the page source. Test-side assertion
  error, not a product defect; the assertion was rewritten to check
  the concatenation form.
- Run 2: **4/5** - the rewritten assertion (`class='col shelf '`)
  still could not match: the JS string closes with a double quote,
  so the asserted closing single quote is absent from the source
  text. Assertion corrected to the exact concatenation text
  (`col shelf "+colShelf[k]+`), keeping the same semantic check -
  board columns are shelves, colored by state from the colShelf map,
  whose four class names are also asserted, along with the two exact
  shelf CSS rules and the static shelf counts.
- Run 3 (final code): **5/5**.

No product behavior was changed to satisfy the gate; both fixes were
to the test's expected string form, and the underlying rendering
(columns as state-colored shelves) is what the assertions verify.

## What v0.5.0 is

A PURE RESKIN of the dashboard in the Trust Ladder design language
(Ryan's pick, approved Nov 1, 2026), plus Ryan's Ghost Agent icon as
the real brand mark (additional approved scope, same day). No API
changes, no new endpoints except the static icon route, no payload
changes (proven by gate K5), no behavior changes. Payload functions
in server.py are untouched; governance and record-before-execute are
untouched.

- Palette: deep navy (#07030f family), panels #120b24 / #0d0820,
  borders #2b2149, Ghost violet #6716f3, light violet #b18cff /
  #a678ff, green #3ecf6e, amber #ffb43a, red #ff5c5c / #ff7b7b.
- Shelves (18px rounded, border = meaning): Overview (Needs You
  dashed amber, Recent), Board (each column a shelf: violet running,
  dashed amber waiting, green done, red failed), Approvals (Pending
  dashed amber, Recent decisions), Memory (Candidates dashed amber,
  Trusted solid green - the Trust Ladder page itself), Skills (same
  two-shelf split, status filter still honored), Schedules (Active
  violet, Paused muted), System (Health green; Auth green with a
  token, amber without).
- Cards: rank chips, text-glyph icon tiles (WRITE / READ / LIST /
  NOTE / EXEC / MEM / SKILL / AUTO - text, never emoji), status
  pills, and provenance strips built only from fields that exist
  (task: model, step n of m, status; approval: source task,
  requested tool + step, effect derived from the tool kind; memory:
  source, status, verified; skill: source, permissions, status).
  Missing fields omit the cell; nothing is fabricated.
- App drawer: all 11 pages exactly once, grouped OPERATE /
  INTELLIGENCE / SYSTEM, live badges mirrored from the sidebar;
  opens from the header All Apps button and the bottom nav's More;
  Esc closes.
- Bottom nav at <=820px: Home / Board / Agents / More, active target
  violet; sidebar hides and content gains bottom padding on those
  widths. Desktop keeps the grouped sidebar.
- Every v0.4.0 feature verified working under the new skin by the
  unchanged v1-v4 suites: 5s refresh preserving page/selection/
  filters, search dropdown, Ctrl/Cmd+K palette, density toggle,
  logs pause, bulk approvals, retry, scheduler actions, chat inline
  approvals, token login box, fail-closed bind.

## Brand icon (approved additional scope)

- Source: `~/workspace/user/files/426_6_girm.png` as Ryan provided
  it. Despite the .png name the file is JPEG data (JFIF, 1254x1254,
  275,438 bytes) with a baked-in black background; it is stored
  byte-identical as `assets/ghost-agent-icon.jpg` (repo root).
- Served at GET /assets/ghost-agent-icon.jpg with Content-Type
  image/jpeg, WITHOUT a token (the login screen and header render
  before auth). Path resolved from server.py's location
  (parents[2] / assets / ghost-agent-icon.jpg), so it works from any
  working directory.
- Fallback: server-side existence check when the page is built
  (_brandmark_html) - if the file is absent, the brand slots render
  the CSS "G" tile instead of an <img>, so there is never a
  broken-image icon.
- Used as: sidebar brand header mark, app drawer header mark, login
  box mark, <link rel="icon"> favicon, and the README hero image at
  the top of README.md.

## Test results

- Gate v5: **5/5** (history above).
- Full suite: **81 passed, 0 failed**
  (`PYTHONPATH=src python3 -m pytest tests/ -q`): the 72 pre-existing
  tests, gate v5 (5), and the new tests/test_server_v5.py (4: version
  in export/system, page landmarks, icon asset served token-free at
  200 + image/jpeg + >100KB + JPEG magic while the API stays 401,
  brand-mark fallback when the icon file is missing).

### Existing-test changes (recorded, none weakened)

1. `tests/test_server_v4.py`: export version assertion
   `"0.4.0"` -> `"0.5.0"`. Reason: the version-bump deliverable
   itself; it was the only hard-coded version in the test tree.
2. Gate v5 K2 assertion form corrected twice during the gate loop
   (see Honest gate history) - test-side string-form fixes; the
   semantic check is unchanged.
3. `tests/test_server_v5.py` asserts the System health landmark in
   its actual JS-rendered form `id='sys-health'` (single-quoted
   attribute, as renderSystem builds it).

No other pre-existing test file was modified; all v2/v3/v4 page
landmarks survived the reskin as-is.

## Demo and smoke

- CLI demo (isolated home /tmp/ga-v5-demo): exit 0; task paused at
  waiting_approval, demo approval granted, 3 steps done,
  demo_output.txt content exactly 'Ghost Agent demo run\n',
  candidate skill proposed, 11 trail events - identical shape to
  v0.4.0.
- Live HTTP smoke (real `serve` process, seeded home): GET / 200
  with the new landmarks (mem-candidates-shelf, drawer, bottomnav,
  brandicon); /api/board, /api/overview, /api/approvals,
  /api/system, /api/memory, /api/skills all 200;
  /assets/ghost-agent-icon.jpg 200 image/jpeg.
- The served page's <script> was extracted from the live response
  (not the .py source) and passed `node --check`.

## Files changed

- `src/ghost_agent/server.py` - reskin (CSS/HTML/JS rendering only)
  + token-free icon route + _brandmark_html fallback; payload
  functions untouched.
- `src/ghost_agent/__init__.py`, `pyproject.toml` - version 0.5.0.
- `assets/ghost-agent-icon.jpg` - NEW, Ryan's icon (JPEG, 1254x1254).
- `tests/test_sanity_gate_v5.py` - NEW, gate v5 (5 cases).
- `tests/test_server_v5.py` - NEW, 4 reskin/icon tests.
- `tests/test_server_v4.py` - version pin 0.4.0 -> 0.5.0 (above).
- `README.md` - hero image, design-language section, v5 test/report
  links.
- `BUILD_REPORT_V5.md` - this report.

No GitHub push, no publish, $0 spend. Still untested (unchanged from
v0.4.0): a live call to a real model provider - no real key or
reachable endpoint exists in this environment; the MockModel
fallback path remains the verified one.
