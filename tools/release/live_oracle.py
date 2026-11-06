"""Exact preprovisioned independent release oracle configuration."""

import json

GITHUB_PATH = ".github/workflows/forge-release-oracle.yml"
JOB = "release-smoke-oracle"
TEST = "tests/test_forge_release_smoke.py"
PYTHON_IMAGE = (
    "python:3.13-slim@sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39"
)


def github(manifest: dict) -> bytes:
    return (
        "name: Forge release oracle\n"
        "on:\n  push:\n    branches: ['forge/**']\n"
        "permissions:\n  contents: read\n  actions: read\n  checks: write\n"
        f"jobs:\n  {JOB}:\n    name: {JOB}\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 10\n    steps:\n"
        "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n"
        "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.13'\n"
        "      - run: python -m pip install --disable-pip-version-check pytest==9.0.2\n"
        f"      - run: python -m pytest {TEST} -q --junitxml=forge-oracle.xml\n"
        "      - name: Publish exact failed native JUnit\n        if: failure()\n"
        "        env:\n          GITHUB_TOKEN: ${{ github.token }}\n"
        "        run: |\n          set -euo pipefail\n"
        f"          curl --fail --location --proto '=https' --proto-redir '=https' --max-filesize 262144 {json.dumps(manifest['verification_reporter_url'])} -o /tmp/forge-junit.py\n"
        f"          printf '%s  /tmp/forge-junit.py\\n' {json.dumps(manifest['verification_reporter_sha256'])} | sha256sum --check --status\n"
        f"          python /tmp/forge-junit.py --report forge-oracle.xml --job-name {JOB}\n"
    ).encode()


def gitlab() -> dict:
    return {
        "include": [{"local": "forge-lane.gitlab-include.yml"}],
        JOB: {
            "image": PYTHON_IMAGE,
            "timeout": "10 minutes",
            "rules": [{"if": '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH =~ /^forge\\//'}],
            "script": [
                "python -m pip install --disable-pip-version-check pytest==9.0.2",
                f"python -m pytest {TEST} -q --junitxml=forge-oracle.xml",
            ],
            "artifacts": {"when": "always", "reports": {"junit": "forge-oracle.xml"}},
        },
    }
