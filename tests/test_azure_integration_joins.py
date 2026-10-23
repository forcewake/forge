"""Portable dispatch/template joins retained on GitHub Actions."""

from pathlib import Path
import yaml
from forge.execution.github_actions import ActionsHandle
from tests.test_github_harness import db as db, fake as fake, get_run, go, make_service, start


async def test_dispatched_inputs_are_declared_and_the_journaled_handle_is_consumable(db, fake):
    service = make_service(db, fake)
    run_id = await start(service)
    await go(service, run_id)
    (dispatch,) = fake.dispatch_inputs
    template = Path(__file__).parent.parent / "ci/templates/forge-harness.github.yml"
    document = yaml.safe_load(template.read_text())
    on = document.get("on", document.get(True))
    declared = set(on["workflow_dispatch"]["inputs"])
    assert set(dispatch["inputs"]) <= declared
    run = await get_run(db, run_id)
    handle = ActionsHandle.from_json(run.evidence["harness"]["handle"])
    assert handle.forge_run_id == run_id
    assert handle.run_id == run.evidence["harness"]["run_id"]
    assert dispatch["inputs"]["spec_digest"] == run.spec_digest
    assert (
        dispatch["inputs"]["envelope_digest"] == run.evidence["brief_envelope"]["envelope_digest"]
    )


def test_github_workflow_matches_the_registry_exactly():
    import re

    from forge.runs.harness_selection import (
        DRIVER_CREDENTIAL_VARS,
        DRIVER_OPTIONAL_CREDENTIAL_VARS,
        SHIPPED_DRIVERS,
    )

    text = (
        Path(__file__).parent.parent / ".github" / "workflows" / "forge-harness.yml"
    ).read_text()
    for driver in SHIPPED_DRIVERS:
        expected = set(DRIVER_CREDENTIAL_VARS[driver])
        granted: set[str] = set()
        for match in re.finditer(
            r"^          ([A-Z_]+): \$\{\{.*?inputs\.driver == '([a-z-]+)'.*?\}\}$",
            text,
            re.M,
        ):
            # One env line may guard SEVERAL drivers (the ANTHROPIC_*
            # lines cover claude-code AND the EXE-02 claude-sdk-lane,
            # which shares the recipe) — credit every arm on the line.
            arms = re.findall(r"inputs\.driver == '([a-z-]+)'", match.group(0))
            if driver in arms:
                granted.add(match.group(1))
        allowed = expected | set(DRIVER_OPTIONAL_CREDENTIAL_VARS.get(driver, ()))
        assert granted <= allowed and granted >= expected, (
            f"{driver}: github workflow grants {granted}, "
            f"registry requires {expected} (allows {allowed})"
        )
