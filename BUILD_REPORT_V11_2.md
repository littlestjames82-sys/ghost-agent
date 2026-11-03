# Ghost Agent v0.11.2 — dashboard scroll fix

Patch release. One functional change, in the dashboard page's
JavaScript inside `src/ghost_agent/server.py` (the `PAGE` refresh
path). No Python runtime behavior changes.

## The bug (reported by Ryan on the Android app, Nov 2, 2026)

Scrolling any dashboard page to the bottom threw the user back to
the top. Mechanism, confirmed from the page source: the 5-second
auto-refresh (`refresh(); setInterval(refresh, 5000)`) rebuilt
every section's innerHTML and ended with `showPage(curPage)`,
which unconditionally ran `window.scrollTo(0, 0)`. Not a page
reload and not a client-side rebuild — the page scrolled itself
to the top on every poll.

## The fix

In `refresh()`:
1. `gaSig()` computes a cheap signature (JSON length + hash) over
   the nine fetched payloads. When the signature is unchanged,
   `refresh()` returns before touching the DOM — no rebuild, no
   scroll disturbance.
2. When the payload did change, `window.scrollY` is captured
   before the render and restored after `showPage` runs
   (`if (gaScrollY>0) window.scrollTo(0,gaScrollY)`).
3. `toggleLogsPause` calls `refresh(true)` so unpausing the logs
   view cannot be frozen by the unchanged-payload guard.

## Verification

- Android gate phase 8b executes the served page's real
  `refresh()` under a node DOM stub against harvested live
  payloads: a second refresh with identical payloads performs
  zero innerHTML writes (SC1), and a changed payload re-renders
  with the final scroll call restoring the pre-render position
  past showPage's reset (SC2). The app snapshot regenerated from
  this source carries the identical page (85,830 bytes).
- Full Python suite after the fix: 163 passed (parent foreground
  run, 46 min), before the version pins moved.
- Version pins: pyproject.toml, `__init__.__version__`, and the
  /api/export + /api/system assertions in test_server_v4.py and
  test_server_v5.py moved 0.11.1 -> 0.11.2; the sanity gate files
  and both server test files re-run green after the bump.
