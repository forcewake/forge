"""A held allowance, a known lower bound and settled usage answer different questions."""

from collections import Counter

from forge.domain.budget import Pool, Resources


def pool_projection(pool: Pool) -> dict[str, object]:
    accrued = Resources()
    states: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    reservations = []
    for row in pool.reservations:
        states[row.status] += 1
        if row.status != "settled" and row.observed is not None:
            accrued = accrued + row.observed.lower_bound
        for axis in ("usd", "calls", "tokens"):
            if row.observed is None or getattr(row.observed, axis) is None:
                unknown[axis] += 1
        reservations.append(
            {
                "identity": row.identity,
                "state": row.status,
                "allowance": row.allowance.model_dump(mode="json"),
                "observed": row.observed.model_dump(mode="json") if row.observed else None,
            }
        )
    return {
        **pool.balance.model_dump(mode="json"),
        "known_accrued_lower_bound": accrued.model_dump(mode="json"),
        "reservation_states": dict(states),
        "unknown_axes": dict(unknown),
        "reservations": reservations,
    }
