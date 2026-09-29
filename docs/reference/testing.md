# Testing forge

## Tiers

| Command | What runs | Where |
| --- | --- | --- |
| `uv run pytest -q -m "not slow"` | The default tier: fast, hermetic | Every pull request (`test` job) |
| `uv run pytest -q -m slow` | Real processes and services (`tests/production_entry`, `tests/fi_os`) | `main` and nightly (`tests-slow` job) |
| `uv run pytest -q -m invariant $(uv run python -m tests.invariants.registry)` | The Invariant Suite I1–I8 | Every pull request (`invariants` job, with Postgres) |
| `uv run pytest -q` | Everything | Locally, before large changes |

Tiers are assigned by path in `tests/_hygiene.py`; the invariant files are
listed in `tests/invariants/registry.py`.

## Subprocess hygiene

Tests that start helper processes must stop them in fixture teardown. Two
guards back this up:

- helper scripts under `tests/production_entry/` call
  `orphan_guard.install()`, so they exit when pytest dies abruptly;
- `pytest_sessionfinish` fails the run if a child process of pytest is
  still running at the end.

Leftovers from older runs (before these guards) can be found and stopped:

```bash
pgrep -fl "tests/production_entry/(fake_native_server|durable_ingress_server|production_matrix_worker)"
kill <pid>   # for each pid listed above
```
