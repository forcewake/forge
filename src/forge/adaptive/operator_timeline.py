"""The operator timeline (R28-29): request, effect and evidence, distinguished.

Control now crosses real process boundaries: an operator's ``/pause`` is a
mailbox row at the control plane, an ack ladder the lane climbs through the
remote channel, a vendor effect the lane alone observes, and a checkpoint
booking that closes the loop. Collapsing those into one word ("paused")
hides exactly the distinctions the operator needs — a recorded ``/resume``
that no runner drained yet, a local-only checkpoint that is not remote
recovery, an effect whose outcome is UNPROVEN. This module is the honest
projection: :func:`timeline_from_journal` is a PURE function over journal
rows — the lane's append-only
:class:`~forge.adaptive.lane_control.SteeringAction` journal, the channel's
error/evidence rows, the control plane's command rows and audit entries —
and every row is categorized as exactly ONE of :data:`TIMELINE_CATEGORIES`:

- ``request_received`` — the command exists at the control plane, awaiting
  authorization (a mailbox row at ``received``/``pending``);
- ``authorized`` — the actor's scopes held (``authorized``);
- ``effect_dispatched`` — the lane INTENDED the vendor effect (the coarse
  ``applied`` rung IS the intent rung, the durable ``dispatching`` names
  it; ``outcome_unknown`` stays here: dispatched, observation unproven);
- ``effect_observed`` — the lane saw the vendor take the effect (an
  applied steering action; ``vendor_accepted`` acks);
- ``checkpoint_committed`` — the application is booked (``checkpointed``);
- ``evidence_recorded`` — a decision or a fact with no effect claim of its
  own: refusals, ignores, decisive errors, expired/rejected rungs, channel
  error rows.

The projection is order-preserving (append-only journals are already
ordered — nothing here re-sorts or invents timing) and shape-agnostic:
dataclasses, pydantic contracts and plain mappings all classify, and a row
that carries nothing classifiable is SKIPPED — malformed input is passed
over honestly, never guessed into a category.

Exposure (R28-29): :meth:`LaneSteeringSession.timeline` projects the lane's
journal (the same rows the lane meta's ``steering_journal`` sidecar
carries, each with its ``at`` timestamp); the command router's
:func:`~forge.adaptive.command_router.control_timeline` projects the
control-plane mailbox evidence into a ``timeline`` section.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

__all__ = [
    "TIMELINE_CATEGORIES",
    "TimelineCategory",
    "TimelineEntry",
    "timeline_from_journal",
    "timeline_rows",
]

#: The six honest categories — what each row PROVES, never more.
TIMELINE_CATEGORIES: Final[tuple[str, ...]] = (
    "request_received",
    "authorized",
    "effect_dispatched",
    "effect_observed",
    "checkpoint_committed",
    "evidence_recorded",
)

TimelineCategory = Literal[
    "request_received",
    "authorized",
    "effect_dispatched",
    "effect_observed",
    "checkpoint_committed",
    "evidence_recorded",
]

#: A ladder word (a mailbox status, an ack state, an audit ``to``) → the
#: category it proves. The coarse in-memory ``applied`` rung maps to
#: ``effect_dispatched`` — it is the INTENT rung (the durable mailbox
#: names the same leg ``dispatching``); ``outcome_unknown`` stays a
#: dispatch claim: the effect left, the observation never came back.
_LADDER_CATEGORY: Final[dict[str, TimelineCategory]] = {
    "received": "request_received",
    "pending": "request_received",
    "authorized": "authorized",
    "dispatching": "effect_dispatched",
    "applied": "effect_dispatched",
    "outcome_unknown": "effect_dispatched",
    "vendor_accepted": "effect_observed",
    "checkpointed": "checkpoint_committed",
    "expired": "evidence_recorded",
    "rejected": "evidence_recorded",
}

#: Where a row's ladder word lives, in lookup order: the durable audit
#: entry's ``to``, the channel ack's ``state``, the command's ``status``.
_LADDER_KEYS: Final = ("to", "state", "status")


@dataclass(frozen=True)
class TimelineEntry:
    """One timeline row: the category, when, which command, what it proves.

    ``at`` is the row's own timestamp verbatim (an ISO string) — ``""``
    when the source row carried none (the ordering is the journal's, never
    a synthesized clock). ``outcome`` is the row's own status/outcome word,
    unrenamed, so the operator can always see the raw rung beside the
    category. ``line`` is the human-readable sentence for /status output.
    """

    category: TimelineCategory
    at: str
    command_id: str
    kind: str
    outcome: str
    line: str


def timeline_from_journal(journal: Iterable[Any]) -> list[TimelineEntry]:
    """Project journal rows into the operator timeline (pure, order-preserving).

    Accepts any iterable of rows —
    :class:`~forge.adaptive.lane_control.SteeringAction` instances, the
    dict rows the lane meta's ``steering_journal`` sidecar carries
    (actions and ``lane_control_error`` channel rows), control-plane
    :class:`~forge.adaptive.models.ControlCommand` rows, and the durable
    mailbox's audit entries (``{"at", "from", "to"}``). Each row yields at
    most ONE entry; a row with nothing classifiable (wrong shape, or no
    outcome and no ladder word) is skipped — never coerced.
    """
    entries: list[TimelineEntry] = []
    for row in journal:
        entry = _entry_for(row)
        if entry is not None:
            entries.append(entry)
    return entries


def timeline_rows(journal: Iterable[Any]) -> list[dict[str, Any]]:
    """The timeline as JSON-safe dicts (the /status and sidecar shape)."""
    return [asdict(entry) for entry in timeline_from_journal(journal)]


# -- row → entry (private) -----------------------------------------------------


def _entry_for(row: Any) -> TimelineEntry | None:
    view = _view_of(row)
    if view is None:
        return None
    if isinstance(view.get("outcome"), str) and view["outcome"]:
        return _action_entry(view)
    for key in _LADDER_KEYS:
        word = view.get(key)
        if isinstance(word, str) and word:
            return _ladder_entry(view, word)
    row_type = view.get("type")
    if (isinstance(row_type, str) and row_type) or "error" in view:
        return _evidence_entry(view, str(row_type or "error"))
    return None


def _view_of(row: Any) -> dict[str, Any] | None:
    """A dict view of one row, or ``None`` when the shape is unrecognizable."""
    if isinstance(row, Mapping):
        return dict(row)
    if dataclasses.is_dataclass(row) and not isinstance(row, type):
        return {field.name: getattr(row, field.name) for field in dataclasses.fields(row)}
    dump = getattr(row, "model_dump", None)
    if callable(dump):
        try:
            payload = dump()
        except Exception:  # noqa: BLE001 — an undumpable row is skipped, not fatal
            return None
        if isinstance(payload, Mapping):
            return dict(payload)
    return None


def _action_entry(view: Mapping[str, Any]) -> TimelineEntry:
    """A journaled steering action — the LANE's own claim, at its strongest."""
    outcome = str(view.get("outcome") or "")
    kind = str(view.get("kind") or "")
    command_id = str(view.get("command_id") or "")
    detail = view.get("detail") if isinstance(view.get("detail"), Mapping) else {}
    mailbox = str(detail.get("mailbox_status") or "")
    reason = str(view.get("reason") or "")
    if outcome == "applied":
        category: TimelineCategory = "effect_observed"
        booking = f"; mailbox: {mailbox}" if mailbox else ""
        line = f"{kind or 'command'} {command_id} applied — the vendor effect was observed{booking}"
    elif outcome == "delivery_unknown":
        category = "effect_dispatched"
        line = (
            f"{kind or 'command'} {command_id} dispatched — outcome UNPROVEN, "
            "a probe must decide before any retry"
        )
    else:
        category = "evidence_recorded"
        why = f" — {reason}" if reason else ""
        line = (
            f"{kind or 'command'} {command_id} {outcome} — journaled decision, no effect claim{why}"
        )
    return _entry(category, view, outcome, line)


def _ladder_entry(view: Mapping[str, Any], word: str) -> TimelineEntry:
    """A ladder word (mailbox status / ack state / audit ``to``)."""
    kind = str(view.get("kind") or "")
    command_id = str(view.get("command_id") or "")
    category = _LADDER_CATEGORY.get(word, "evidence_recorded")
    lines: dict[str, str] = {
        "request_received": f"{kind or 'command'} {command_id} recorded — awaiting the lane",
        "authorized": f"{kind or 'command'} {command_id} authorized — the actor's scopes held",
        "effect_dispatched": f"{kind or 'command'} {command_id} dispatched to the lane ({word})",
        "effect_observed": f"{kind or 'command'} {command_id} — the vendor took the effect",
        "checkpoint_committed": f"{kind or 'command'} {command_id} checkpointed — the application is booked",
        "evidence_recorded": f"{kind or 'command'} {command_id} — ladder word {word!r}, no effect claim",
    }
    return _entry(category, view, word, lines[category])


def _evidence_entry(view: Mapping[str, Any], row_type: str) -> TimelineEntry:
    """A channel/evidence row (an error, a note) — recorded, never an effect."""
    error = str(view.get("error") or view.get("note") or "")
    line = f"{row_type}: {error}" if error else row_type
    return _entry("evidence_recorded", view, row_type, line)


def _entry(
    category: TimelineCategory, view: Mapping[str, Any], outcome: str, line: str
) -> TimelineEntry:
    return TimelineEntry(
        category=category,
        at=str(view.get("at") or ""),
        command_id=str(view.get("command_id") or ""),
        kind=str(view.get("kind") or ""),
        outcome=outcome,
        line=line,
    )
