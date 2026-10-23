"""Admission and configuration failures for the required CI execution lane."""

from dataclasses import dataclass

import yaml

ONBOARDING_URL = "https://github.com/forcewake/forge/blob/main/docs/harnesses/onboarding.md"
CODE_MODEL_TIER = "code"


@dataclass(frozen=True)
class LaneRefusal:
    detail: str
    code: str = "lane_not_onboarded"
    onboarding_url: str = ONBOARDING_URL

    @property
    def reason(self) -> str:
        return f"{self.code}: {self.detail[:60]}; see {self.onboarding_url}"

    def as_document(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "onboarding_url": self.onboarding_url}


class LaneRequiredError(ValueError):
    """The configured backend cannot execute a CI lane."""

    def __init__(self, detail: str) -> None:
        self.refusal = LaneRefusal(detail)
        super().__init__(self.refusal.reason)


def lane_driver(backend: str) -> str:
    kind, _, driver = str(backend or "").strip().partition(":")
    if kind != "ci_harness":
        raise LaneRequiredError("implementation requires ci_harness[:driver]")
    return driver.strip() or "claude-code"


async def gitlab_lane_admission(
    client,
    project_id: int,
    ref: str,
    backend: str,
) -> tuple[LaneRefusal | None, set[str]]:
    from forge.runs.harness_selection import SHIPPED_DRIVERS

    try:
        driver = lane_driver(backend)
    except LaneRequiredError as exc:
        return exc.refusal, set()
    try:
        configuration = await client.lint_ci_configuration(project_id, ref)
    except Exception as exc:
        return LaneRefusal(f"project CI configuration read failed ({type(exc).__name__})"), set()
    if not isinstance(configuration, dict) or configuration.get("valid") is not True:
        return LaneRefusal("project CI configuration is invalid or unavailable"), set()
    jobs = configuration.get("jobs")
    if not isinstance(jobs, list):
        return LaneRefusal("project CI configuration returned no job inventory"), set()
    names = {
        job.get("name") for job in jobs if isinstance(job, dict) and job.get("when") != "never"
    }
    available = {
        name for name in SHIPPED_DRIVERS if f"forge-agent-{name.removesuffix('-lane')}" in names
    }
    if "forge-agent" in names:
        available.add(driver)  # the existing single-template installation
    if driver not in available:
        return LaneRefusal(f"project CI configuration has no execution job for {driver}"), set()
    return None, available


async def github_lane_refusal(
    reader, project_id: int, ref: str, workflow: str
) -> LaneRefusal | None:
    if not workflow or "/" in workflow or "\\" in workflow or workflow in {".", ".."}:
        return LaneRefusal("project has no configured GitHub Actions workflow")
    try:
        result = await reader.read_blob(project_id, f".github/workflows/{workflow}", ref=ref)
    except Exception as exc:
        return LaneRefusal(f"project workflow could not be read ({type(exc).__name__})")
    if result.status != "found":
        return LaneRefusal(f"project workflow read returned {result.status}")
    try:
        document = yaml.load(result.text(), Loader=yaml.BaseLoader)
    except (yaml.YAMLError, ValueError):
        return LaneRefusal("project workflow is not valid YAML")
    trigger = document.get("on") if isinstance(document, dict) else None
    dispatchable = trigger == "workflow_dispatch" or (
        isinstance(trigger, (dict, list)) and "workflow_dispatch" in trigger
    )
    if not dispatchable or not isinstance(document.get("jobs"), dict) or not document["jobs"]:
        return LaneRefusal("project workflow has no manual execution jobs")
    return None
