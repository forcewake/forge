"""Ordinary lifecycle edges and explicit revival into the failed handler's owner."""

from enum import StrEnum
from types import MappingProxyType


class State(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    PUBLISHING = "publishing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    READY_FOR_HUMAN = "ready_for_human"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Handler(StrEnum):
    PLAN = "plan"
    APPROVE = "approve"
    DISPATCH = "dispatch"
    AWAIT_LANE = "await_lane"
    PUBLISH = "publish"
    VERIFY = "verify"
    REVIEW = "review"
    READY = "ready"


TERMINAL = frozenset({State.READY_FOR_HUMAN, State.BLOCKED, State.FAILED, State.CANCELLED})
_PARK = frozenset({State.BLOCKED, State.FAILED, State.CANCELLED})
_FORWARD = {
    State.QUEUED: frozenset({State.PLANNING}),
    State.PLANNING: frozenset({State.AWAITING_APPROVAL}),
    State.AWAITING_APPROVAL: frozenset({State.RUNNING}),
    State.RUNNING: frozenset({State.PUBLISHING}),
    State.PUBLISHING: frozenset({State.VERIFYING}),
    State.VERIFYING: frozenset({State.RUNNING, State.REVIEWING}),
    State.REVIEWING: frozenset({State.READY_FOR_HUMAN}),
}
TRANSITIONS = MappingProxyType(
    {
        state: _FORWARD.get(state, frozenset()) | (_PARK if state not in TERMINAL else frozenset())
        for state in State
    }
)
HANDLER_STATE = MappingProxyType(
    {
        Handler.PLAN: State.PLANNING,
        Handler.APPROVE: State.AWAITING_APPROVAL,
        Handler.DISPATCH: State.RUNNING,
        Handler.AWAIT_LANE: State.RUNNING,
        Handler.PUBLISH: State.PUBLISHING,
        Handler.VERIFY: State.VERIFYING,
        Handler.REVIEW: State.REVIEWING,
        Handler.READY: State.REVIEWING,
    }
)
LEGACY_STATES = MappingProxyType(
    {
        "accepted": State.QUEUED,
        "preflight": State.QUEUED,
        "planning": State.PLANNING,
        "waiting_approval": State.AWAITING_APPROVAL,
        "proposing": State.RUNNING,
        "waiting_harness": State.RUNNING,
        "validating": State.PUBLISHING,
        "committing": State.PUBLISHING,
        "ensuring_draft_mr": State.PUBLISHING,
        "waiting_ci": State.VERIFYING,
        "evaluating_ci": State.VERIFYING,
        "reviewing": State.REVIEWING,
        "ready_for_human": State.READY_FOR_HUMAN,
        "blocked": State.BLOCKED,
        "failed": State.FAILED,
        "cancelled": State.CANCELLED,
    }
)
REVIVAL_STATE = MappingProxyType({**HANDLER_STATE, Handler.PUBLISH: State.RUNNING})


class InvalidTransition(ValueError):
    pass


def transition(current: State, target: State) -> State:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"{current} cannot transition to {target}")
    return target


def revive(current: State, failed_handler: Handler) -> State:
    if current not in (State.BLOCKED, State.FAILED):
        raise InvalidTransition(f"{current} cannot revive")
    return REVIVAL_STATE[failed_handler]
