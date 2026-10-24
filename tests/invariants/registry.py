"""The Invariant Suite registry (DESIGN §4).

Each invariant names the test files that prove it. The collection hook in
``tests/conftest.py`` marks every test in these files, and every test under
``tests/invariants/``, with ``invariant``. ``test_registry_integrity``
fails when a listed file disappears, so evidence cannot vanish silently.

One registry rather than one module per invariant: most evidence already
lives in focused test files. ``tests/test_failure_injection_os.py`` is not
listed: it needs real workers and Redis and runs nightly in the
``integration-os`` job, while the ``invariants`` job fails on any skip.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Invariant:
    ident: str
    rule: str
    files: tuple[str, ...]


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        "I1",
        "The bot never merges; the lane holds no write credential",
        (
            "tests/invariants/test_i1_no_merge.py",
            "tests/test_gitlab_engine_ports.py",
        ),
    ),
    Invariant(
        "I2",
        "Only the publisher writes, and every write is validated",
        (
            "tests/test_publication_boundary.py",
            "tests/test_publisher.py",
            "tests/test_patch_differential.py",
            "tests/test_publication_manifest.py",
            "tests/test_native_git_publication.py",
            "tests/test_github_native_commit.py",
            "tests/test_domain_binary_candidate.py",
            "tests/test_engine_delivery.py",
        ),
    ),
    Invariant(
        "I3",
        "Intent before effect; unknown outcomes are probed, never retried blindly",
        (
            "tests/test_publication_intents.py",
            "tests/test_failure_injection.py",
            "tests/test_engine_effects.py",
            "tests/test_engine_lane_handlers.py",
            "tests/test_engine_delivery.py",
            "tests/test_engine_commands.py",
        ),
    ),
    Invariant(
        "I4",
        "Approval is consume-once and digest-bound; the executor gets the approved bytes",
        (
            "tests/test_durable_gates.py",
            "tests/test_runs_spec.py",
            "tests/test_revision_executor_proof.py",
            "tests/test_engine_lane.py",
            "tests/test_engine_approval.py",
            "tests/test_engine_delivery.py",
            "tests/test_engine_operator.py",
            "tests/test_server_model_services.py",
        ),
    ),
    Invariant(
        "I5",
        "Budget before spend; idempotent receipts; unknown spend is never zero",
        (
            "tests/test_budgets.py",
            "tests/test_closing_budget.py",
            "tests/test_budget_amendment.py",
            "tests/test_usage_receipts.py",
            "tests/test_budget_engine.py",
            "tests/test_engine_delivery.py",
            "tests/test_engine_commands.py",
            "tests/test_server_models.py",
            "tests/test_server_model_services.py",
        ),
    ),
    Invariant(
        "I6",
        "Exactly one Draft PR/MR per run",
        (
            "tests/production_entry/test_concurrent_publication.py",
            "tests/test_engine_delivery.py",
        ),
    ),
    Invariant(
        "I7",
        "A killed worker loses no work and duplicates no effect",
        (
            "tests/test_failure_injection.py",
            "tests/test_engine_effects.py",
            "tests/test_engine_runner.py",
            "tests/test_engine_delivery.py",
            "tests/test_engine_commands.py",
            "tests/test_engine_dispatch_intents.py",
            "tests/test_engine_operator.py",
        ),
    ),
    Invariant(
        "I8",
        "Verification is bound to the exact candidate SHA",
        (
            "tests/test_runs_verification.py",
            "tests/test_verification_binding.py",
            "tests/test_engine_delivery.py",
            "tests/test_gitlab_engine_ports.py",
            "tests/test_text_reviewer.py",
        ),
    ),
    Invariant(
        "SELF-TRIGGER",
        "Bot-authored notes are never commands",
        (
            "tests/test_bot_loop.py",
            "tests/test_github_webhook.py",
            "tests/test_gitlab_engine_ingress.py",
        ),
    ),
)

INVARIANT_FILES: frozenset[str] = frozenset(f for inv in INVARIANTS for f in inv.files)


if __name__ == "__main__":
    # CI selects the suite by file so unrelated module-level skips never count.
    print(" ".join(["tests/invariants", *sorted(INVARIANT_FILES)]))
