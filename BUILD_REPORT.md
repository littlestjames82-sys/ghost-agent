# Ghost Agent v1 — Build Report

Built Nov 1, 2026. Local build + verification only: no GitHub push, no
publish, no release, $0 spend. Python stdlib only, zero dependencies.

## Sanity gate (known answers) — run FIRST, reported separately

Command: `PYTHONPATH=src python3 -m pytest tests/test_sanity_gate.py -v`

**Pass rate: 4/4 (100%)**

| Case | Known answer | Result |
|---|---|---|
| K1 read_file on existing fixture | governance allow, execution succeeds | PASS |
| K2 write_file with NO approval | blocked / waiting_approval, file ABSENT afterward | PASS |
| K3 write_file WITH approval | executes, exact content, trail record exists and precedes execution (trail ts_ns ≤ file mtime_ns) | PASS |
| K4 trail-write fault injection (trail path made a directory) | action does NOT execute, run fails with a trail error | PASS |

Gate history, honestly: the first gate run was 3/4 — K3 failed because
the MockModel parser kept a leading colon in file content for the
"write X with: …" phrasing (`": exact-content-123"` written instead of
`"exact-content-123"`). The parser regex in `models.py` was fixed, the
failed run's results were discarded, and the gate was re-run from
scratch: 4/4. Only then was the broader suite run.

## Full test suite

Command: `PYTHONPATH=src python3 -m pytest tests/ -q`

**27 passed, 0 failed** (this count includes the 4 gate tests).

Coverage: store (put/get/list/update, timestamps, trail append/read,
trail raising when unwritable, GHOST_AGENT_HOME), governance
classification + deny-by-default, memory candidate → approve/reject,
supersede, router mock + provider-failure fallback recorded in trail,
MockModel determinism, loop happy path, loop approval path, loop deny
path, unknown-tool denial, sandbox-escape refusal, skill proposal +
promotion, dashboard board payload shape, dashboard page + warning,
and the full HTTP API flow (create → waiting → approve → done, deny).

## End-to-end demo (verified in an isolated GHOST_AGENT_HOME)

`GHOST_AGENT_HOME=/tmp/ga-demo PYTHONPATH=src python3 -m ghost_agent demo`
exited 0: task paused at `waiting_approval` for write_file, demo
approved it, all 3 steps done, `demo_output.txt` present in the
sandbox workspace, a candidate (untrusted) skill was proposed, and
11 trail events were recorded for the task.

## Files created

```
pyproject.toml
README.md
BUILD_REPORT.md
src/ghost_agent/__init__.py    src/ghost_agent/__main__.py
src/ghost_agent/store.py       src/ghost_agent/memory.py
src/ghost_agent/governance.py  src/ghost_agent/tools.py
src/ghost_agent/models.py      src/ghost_agent/agent.py
src/ghost_agent/skills.py      src/ghost_agent/cli.py
src/ghost_agent/server.py
tests/conftest.py
tests/test_sanity_gate.py      tests/test_store.py
tests/test_governance_memory.py
tests/test_agent_router.py     tests/test_server.py
```

## Re-run everything (one sequence)

```sh
cd ~/workspace/ghost-agent
PYTHONPATH=src python3 -m pytest tests/test_sanity_gate.py -v   # gate first: expect 4 passed
PYTHONPATH=src python3 -m pytest tests/ -q                      # full suite: expect 27 passed
PYTHONPATH=src python3 -m ghost_agent demo                      # end-to-end demo: expect exit 0
PYTHONPATH=src python3 -m ghost_agent serve                     # dashboard: http://127.0.0.1:8770/
```

## Notes / downgrades

* No findings were downgraded. One real bug was found BY the gate
  (the colon-in-content parser bug above); it was fixed in code and
  the gate re-run, not waived.
* The real-model adapter is exercised only through the router
  fallback path with a simulated failing provider — no live provider
  call was made (no key was supplied, and none is stored or logged).
* Dashboard approve/deny over real HTTP is covered in
  `tests/test_server.py` against an ephemeral localhost port.
