---
name: forge-setup
description: Set up the current Forge workspace and verify its sole runtime.
---

# Forge development setup

1. Check Python 3.13+ and uv, then run `uv sync --all-packages --extra postgres --extra claude`.
2. Follow the current owner direction in AGENTS.md for verification scope.
   The default tier remains `python -m pytest -q -m "not slow"`; report its
   actual status. Do not hide historical unported-test failures or restore
   a deleted runtime to make them import.
3. Run Ruff, `mypy --strict src/forge packages/forge-protocol/src packages/forge-lane/src`,
   `lint-imports`, `python scripts/check_authority_boundaries.py pyproject.toml`,
   `python scripts/ratchet.py` and `python -m forge_protocol.schema --check`.
4. The current environment oracle is `forge --config forge.toml doctor`.
   It checks the explicitly selected configuration, installed schema,
   configured native repositories and prospective native gateway guards.
   It performs no model request or provider write. A clean clone without
   configured credentials has no live environment pass to report.
5. Installation is explicit: `forge --config forge.toml install` is only
   for an empty database. Startup checks the schema without DDL. Preserve
   an owner's existing environment and lab. Use an owned disposable
   database for installation and invariant checks.

Report the checks actually performed and distinguish local artifact/synthetic
workflow evidence from hosted CI and live provider acceptance. Setup itself
does not authorize commits, pushes, tags, publication or operational changes.
