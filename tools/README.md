# Development and lab tools

These tools run from the repository checkout and are excluded from the product wheel.
Use the separate project environment, for example:

```sh
uv run --project tools python tools/inventory_lab.py --help
uv run --project tools python tools/align_lab.py --help
uv run --project tools python tools/setup_gitlab.py --help
uv run --project tools python tools/driver_live_smoke.py --help
uv run --project tools python tools/gh_live_smoke.py --help
uv run --project tools python tools/canary_smoke.py --help
```

Inventory observes the existing lab. Alignment, project setup and live smokes
have explicit effects when run beyond their help or dry-run mode; review the
arguments and obtain the relevant owner authorization before a live execution.
The release workflow uses `release/` to verify built artifacts and their exact
image, installation and native-provider evidence. Program guardrails and the
required PostgreSQL gate remain under `scripts/`.
