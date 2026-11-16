# Development and lab tools

These tools run from the repository checkout and are excluded from the product wheel.
Use the separate project environment, for example:

```sh
uv run --project tools python tools/inventory_lab.py --help
uv run --project tools python tools/align_lab.py --help
uv run --project tools python tools/setup_gitlab.py --help
uv run --project tools python tools/canary_smoke.py --help
```

Inventory observes the existing lab. Alignment and project setup have explicit
effects when run beyond their help or dry-run mode; review the arguments and
obtain the relevant owner authorization before a live execution. Schema
installation is `forge install` (`python -m forge.store.schema init`). The
worker command is `forge worker`. GitLab credentials come from `GITLAB_URL`
and `GITLAB_TOKEN`. The release workflow uses `release/` to verify built
artifacts and their exact image, installation and native-provider evidence.
Program guardrails and the required PostgreSQL gate remain under `scripts/`.
