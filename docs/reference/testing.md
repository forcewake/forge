# Testing forge

## Tiers

| Command | What runs | Where |
| --- | --- | --- |
| `uv run pytest -q -m "not slow"` | The default tier: fast, hermetic | Every pull request and push (`test` job, Python 3.13 and 3.14) |
| `uv run pytest -q -m slow` | Real processes and services: `tests/production_entry/` and `tests/test_failure_injection_os.py` (which drives `tests/fi_os/`) | Pushes to `main` and `release/**`, nightly and manual runs (`tests-slow` job, Python 3.13 and 3.14) |
| `uv run pytest -q $(uv run python -m tests.invariants.registry)` | The Invariant Suite I1–I8 | Every pull request (`invariants` job, with Postgres) |
| `uv run pytest -q` | Everything | Locally, before large changes |

Tiers are assigned by path in `tests/_hygiene.py`; the invariant files are
listed in `tests/invariants/registry.py`. On pull requests the
production-entry suites also run inside `scripts/pg_gate.py` (the
`integration` job).

## Subprocess hygiene

Tests that start helper processes must stop them in fixture teardown. Two
guards back this up:

- helper scripts under `tests/production_entry/` call
  `orphan_guard.install()`, so they exit when pytest dies abruptly;
- after fixture teardown, `pytest_sessionfinish` kills every direct child
  of pytest that is still running, prints its command line and fails an
  otherwise green run. It needs `pgrep`; without it the check is skipped.

Known gaps (ROADMAP P0-04):

- `scripts/pg_gate.py` does not fail on the leaked-process check yet, so a
  leak in the production-entry suites is caught after merge, by
  `tests-slow`;
- `redis-server` and the `python -m forge.worker` processes started by
  the slow suites are not guarded against an abrupt pytest exit.

Leftovers from older runs can be found and stopped:

```bash
pgrep -fl "tests/production_entry/"   # macOS; on Linux use pgrep -af
kill <pid>                            # for each pid listed above
```
