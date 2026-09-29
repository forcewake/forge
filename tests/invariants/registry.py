"""The Invariant Suite registry (DESIGN §4).

Each invariant names the test files that prove it. The collection hook in
``tests/conftest.py`` marks every test in these files, and every test under
``tests/invariants/``, with ``invariant``. ``test_registry_integrity``
fails when a listed file disappears, so evidence cannot vanish silently.
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
        ("tests/invariants/test_i1_no_merge.py",),
    ),
    Invariant(
        "I2",
        "Only the publisher writes, and every write is validated",
        (
            "tests/test_publication_boundary.py",
            "tests/test_publisher.py",
            "tests/test_patch_differential.py",
        ),
    ),
    Invariant(
        "I3",
        "Intent before effect; unknown outcomes are probed, never retried blindly",
        ("tests/test_publication_intents.py", "tests/test_failure_injection.py"),
    ),
    Invariant(
        "I4",
        "Approval is consume-once and digest-bound; the executor gets the approved bytes",
        (
            "tests/test_durable_gates.py",
            "tests/test_runs_spec.py",
            "tests/test_revision_executor_proof.py",
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
        ),
    ),
    Invariant(
        "I6",
        "Exactly one Draft PR/MR per run",
        ("tests/production_entry/test_concurrent_publication.py",),
    ),
    Invariant(
        "I7",
        "A killed worker loses no work and duplicates no effect",
        ("tests/test_failure_injection.py",),
    ),
    Invariant(
        "I8",
        "Verification is bound to the exact candidate SHA",
        ("tests/test_runs_verification.py", "tests/test_verification_binding.py"),
    ),
    Invariant(
        "SELF-TRIGGER",
        "Bot-authored notes are never commands",
        ("tests/test_bot_loop.py",),
    ),
)

INVARIANT_FILES: frozenset[str] = frozenset(f for inv in INVARIANTS for f in inv.files)


if __name__ == "__main__":
    # CI selects the suite by file so unrelated module-level skips never count.
    print(" ".join(["tests/invariants", *sorted(INVARIANT_FILES)]))
