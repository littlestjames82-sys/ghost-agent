# BUILD REPORT — Ghost Agent v0.11.1 (test-hardening patch)

Tests-only patch. **No runtime behavior changes** — the single change under
`src/` is the version string. Nothing was pushed: no push script was run and
no git/GitHub write of any kind was made. The parent verifies and pushes.

## What changed

**`tests/test_sanity_gate.py` — K3 rewritten (same test, same name, same
location, still one test, so every wrapper count assertion such as
"77 passed" stays valid).**

The old K3 proved record-before-execute by comparing the trail record's
`ts_ns` against the written file's `st_mtime_ns`. That cross-clock
nanosecond comparison is inherently racy: it intermittently failed (~0.1 ms)
under machine load inside the nested prior-gates wrappers during the
v0.10.0/v0.11.0 verifications, while passing standalone every time. At
same-process sequential-write timescales the clock comparison cannot
reliably distinguish trail-first from file-first anyway — the trail's own
append order is the stronger and stable proof.

New K3 asserts, for the approved write task:
- (a) a `tool_execute` trail event exists for the write_file step;
- (b) in trail order for that task, `approval_requested` comes before
  `approval_granted` comes before `tool_execute` (record-before-execute
  as sequence);
- (c) the file exists with the exact expected content;
- (d) loose sanity only: the tool_execute event's `ts_ns` falls within the
  task's run window (at or after the task's first trail event's `ts_ns`,
  at or before a `time.time_ns()` reading taken in the test) — a
  single-clock check; **no filesystem timestamp is read or compared
  anywhere in the file anymore**.

Also in that file: the module docstring's K3 line now describes the
sequence proof, the now-unused `import os` was dropped, and `import time`
added. K1, K2, and K4 are byte-untouched — K4 (trail-write fault injection
stops execution) remains the fail-closed proof.

**Version pins 0.11.0 -> 0.11.1** (nothing else):
- `src/ghost_agent/__init__.py` (1)
- `pyproject.toml` (1)
- `tests/test_server_v4.py` (1)
- `tests/test_server_v5.py` (2)

README untouched: it has no changelog-style section, so the optional
"Hardening" line was skipped per the patch rules. Remaining "0.11.0"
strings in `src/` docstrings/comments and the README are historical
feature annotations (when the v0.11.0 features landed), not version pins.

## Negative control (one-off, not committed)

Scratch script (kept out of the tree) ran the **real** K3 test function
three times on fresh stores. The agent runtime never calls
`store.read_trail` (only CLI/server display paths do), so wrapping
`read_trail` doctored exactly what K3 sees without disturbing the run:

- POSITIVE (undoctored trail): K3 **passed**, as expected.
- CONTROL A (tool_execute events deleted from the trail K3 reads): K3
  **failed** as required — `AssertionError: no tool_execute trail record
  for write_file` (assertion a fires).
- CONTROL B (approval_granted and tool_execute swapped in trail order):
  K3 **failed** as required — `AssertionError: trail order is not
  approval_requested -> approval_granted -> tool_execute; the execution
  was not recorded before it ran` (assertion b fires).

The new K3 still catches both violation shapes the old one guarded.

## Scan of the other gate files (item 5)

Every gate/server/store test file was scanned for cross-clock
timestamp-vs-filesystem assertions (`ts_ns`, `st_mtime`, `getmtime`,
mtime comparisons, "precede" claims). Findings:

- **The K3 pattern existed only in `tests/test_sanity_gate.py`** and is
  now removed. No other file compares a trail/event timestamp against a
  filesystem timestamp, in any wrapper-era file or elsewhere.
- Related but **not** the same pattern — left unchanged:
  - `tests/test_sanity_gate_v9.py` K4 asserts
    `res["newest_mtime"] == expected_iso`, where `expected_iso` is
    derived from a fixture mtime the test itself sets with `os.utime`
    to a fixed constant (1767225600 = 2026-01-01T00:00:00Z). That is a
    value-equality check against a test-controlled constant, not an
    ordering comparison between two live clocks; it is deterministic.
  - `tests/test_sanity_gate_v3.py` (line ~144) contains a synthetic
    trail-entry fixture with `"ts_ns": 1` — fixture data, no comparison.
  - Scheduler tests (`tests/test_sanity_gate_v4.py`,
    `tests/test_server_v4.py`) inject fixed datetimes into `tick()` and
    assert equality on computed ISO strings — deterministic, no live
    clocks.
  - `tests/test_store.py::test_entities_have_timestamps` checks timestamp
    presence/shape only.

## Verification (exact counts from this build's runs)

- Negative control: positive PASS; control A FAIL (intended assertion);
  control B FAIL (intended assertion).
- **Full suite** (`pytest tests/ -q`): **163 passed in 2398.59s
  (0:39:58)**, exit 0 — zero failures, including every nested prior-gates
  wrapper (P11 included) with the new sequence-based K3 in place.
- **Gate v1 + v11** (`pytest tests/test_sanity_gate.py
  tests/test_sanity_gate_v11.py -q`): **26 passed in 1137.21s
  (0:18:57)**, exit 0 (4 tests in v1 + 22 in v11). Disclosure: the first
  attempt at this run was killed about 15 minutes in by a VM/runtime
  restart (uptime confirmed 0 min; the session and its output were
  lost); it was re-run from scratch with output tee'd to a durable log
  (`~/workspace/ghost-agent-v111-run2.log`), and the counts above are
  from that completed re-run.

The parent re-runs the full suite as the back-to-back second green.
