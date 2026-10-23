"""Explicit dispositions for features deferred from this version."""

REMOVED_COMMANDS = frozenset({"security_triage", "debug_ci", "debug_pipeline"})
DEFERRED_NOTE_COMMANDS = frozenset({"/security"})


def unavailable_reply(feature: str) -> dict[str, str]:
    return {
        "status": "refused",
        "refusal_reason": "feature_not_available",
        "feature": feature,
        "message": f"{feature} is not available in this version.",
    }
