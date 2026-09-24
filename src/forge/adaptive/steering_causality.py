#!/usr/bin/env python3
"""The steering-causality PURE GRADER (R37-10 / issue #291, AT-10 →
R37-19 / #300).

The lab pilot's own limitation (#275): the scripted vendor's turn
completes before any poll cadence, so the operator's steer landed as an
honest ``error`` journal row — the vendor's edits were NOT caused by the
guidance. This module owns the machinery that makes causality PROVABLE:

- :func:`grade_causality` — the PURE three-arm grader. An operator
  instruction is CAUSAL on a vendor's subsequent behavior only when all
  three arms hold:

  1. **ordering** — the steer command's durable ACK precedes the
     vendor's subsequent edit events, and the vendor PROVABLY consumed
     the guidance mid-turn (a ``steer_consumed`` event naming the
     durable command id, from the vendor's own append-only event log —
     one clock, one process — after the durable row's journal
     timestamps);
  2. **counterfactual** — the same task run WITHOUT the steer produces
     a DIFFERENT edit set (the scripted-causal vendor's determinism
     makes the counterfactual arm runnable, not guessed);
  3. **semantic target** — the edit MATCHES the instruction's checkable
     transformation (``rename X to Y in path`` → the final file carries
     ``Y`` and no longer carries ``X``, and the unsteered arm did NOT
     perform it on its own).

  Any arm failing names its reason; ``causal`` is never a judgment call.

- The REACTIVE SCRIPTED VENDOR EXECUTABLE (``scripted-causal``) moved
  OUT (R37-19/#300) to :mod:`forge.adaptive.reference.reactive_vendor`
  — the labelled evaluation package — together with the frozen
  demonstration task and the checkable-instruction grammar it applies
  (ONE decision: the grader's arm 3 consumes the same grammar). The
  compatibility re-export below keeps every old attribute path
  (``steering_causality.vendor_main``, the env knobs, the task
  constants, the grammar) working while callers drain to the reference
  path. The spawn contract is unchanged: the lane subprocess's
  ``CODEX_BINARY`` still points at THIS file (``python <this-file>
  app-server``), whose ``__main__`` block loads the stdlib-only
  reference module BY PATH — under ``/usr/bin/env python3``, where no
  forge import is guaranteed to resolve.

This module is STDLIB-ONLY by design: the ``__main__`` spawn arm must
survive an environment with no forge package importable at all.

Not graded here (and never guessable): the revision/WIP/replay legs of
AT-10 — those are proven by the production-entry trace
(``tests/production_entry/test_causal_steering.py``) over the REAL
revision lifecycle in :mod:`forge.adaptive.revisions`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # The grammar moved with the scenario to the reference package; the
    # grader composes it (arm 3 grades with the SAME parse the vendor
    # applies — one decision, one module). TYPE_CHECKING keeps this
    # module's import stdlib-only; the runtime lookup is the lazy import
    # inside grade_causality, and the compatibility re-export below.
    from forge.adaptive.reference.reactive_vendor import SemanticTarget

#: The scenario symbols whose old ``steering_causality.<name>`` paths the
#: lazy re-export below keeps alive (R37-19/#300) — the frozen task, the
#: grammar, the provenance label, the env knobs, and the executable
#: entry. Pinned exhaustively by ``tests/test_reference_separation.py``:
#: removing one fails that test.
_MOVED_SCENARIO_SYMBOLS = frozenset(
    {
        "ACTIONS_ENV",
        "BASE_POLICY_CONTENT",
        "DEFAULT_ACTIONS_ENV",
        "DEFAULT_FIRST_EDIT",
        "DEFAULT_FOLLOWUP_CONTENT",
        "EVENTLOG_ENV",
        "FOLLOWUP_PATH",
        "POLICY_PATH",
        "POLL_MODE_ENV",
        "SCRIPTED_CAUSAL_PROVENANCE",
        "STEER_POLL_INTERVAL_S_ENV",
        "STEER_WAIT_S_ENV",
        "STEERING_DISABLED_ENV",
        "STEERING_TASK_BRIEF",
        "STEERING_TASK_INSTRUCTION",
        "SemanticTarget",
        "apply_instruction",
        "apply_rename",
        "parse_instruction",
        "run_app_server",
        "run_once",
        "vendor_main",
    }
)

__all__ = [
    "CausalityArm",
    "CausalityGrade",
    "EditSet",
    "SteeringCommandEvidence",
    "SteeringRun",
    "VendorEvent",
    "command_evidence_of_row",
    "edits_differ",
    "edit_set_of",
    "grade_causality",
    "read_vendor_events",
] + sorted(_MOVED_SCENARIO_SYMBOLS)


def __getattr__(name: str) -> Any:
    """The R37-19 (#300) compatibility re-export — LAZY on purpose.

    This module doubles as the lane subprocess's spawned FILE (see the
    ``__main__`` block): a top-level forge import would break the spawn
    under ``/usr/bin/env python3``, where no forge import is guaranteed
    to resolve. Lazy resolution keeps the spawn arm stdlib-only while
    the old attribute paths keep working for tests, the qualification
    runner and any external caller.
    """
    if name in _MOVED_SCENARIO_SYMBOLS:
        from forge.adaptive.reference import reactive_vendor

        return getattr(reactive_vendor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_MOVED_SCENARIO_SYMBOLS})


# ---------------------------------------------------------------------------
# The grader's inputs (pure records over captured evidence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteeringCommandEvidence:
    """The steer command as the DURABLE journal recorded it.

    ``received_at`` is the mailbox row's first journal timestamp (the
    operator's acceptance — the ACK); ``authorized_at`` the lane gate's
    authorization hop when it ran (empty when the vendor consumed the
    row straight off the received rung, which the HTTP poll surface
    legitimately serves). Both are ISO-8601 strings from one database
    clock.
    """

    command_id: str
    kind: str
    text: str
    status: str
    received_at: str
    authorized_at: str = ""
    journal: tuple[dict[str, Any], ...] = ()

    @classmethod
    def of_row(cls, row: Any) -> SteeringCommandEvidence:
        """Build the evidence from a ``ControlCommandRow``-shaped object."""
        journal = tuple(dict(entry) for entry in (getattr(row, "journal", None) or []))
        received_at = str((journal[0] or {}).get("at", "")) if journal else ""
        authorized_at = next(
            (str(entry.get("at", "")) for entry in journal if entry.get("to") == "authorized"),
            "",
        )
        return cls(
            command_id=str(row.id),
            kind=str(row.kind),
            text=str((row.payload or {}).get("text", "")),
            status=str(row.status),
            received_at=received_at,
            authorized_at=authorized_at,
            journal=journal,
        )


def command_evidence_of_row(row: Mapping[str, Any]) -> SteeringCommandEvidence:
    """Build the evidence from a serialized row document (tests/records)."""
    journal = tuple(dict(entry) for entry in (row.get("journal") or []))
    received_at = str((journal[0] or {}).get("at", "")) if journal else ""
    authorized_at = next(
        (str(entry.get("at", "")) for entry in journal if entry.get("to") == "authorized"),
        "",
    )
    return SteeringCommandEvidence(
        command_id=str(row.get("command_id") or row.get("id") or ""),
        kind=str(row.get("kind") or ""),
        text=str((row.get("payload") or {}).get("text", "")),
        status=str(row.get("status") or ""),
        received_at=received_at,
        authorized_at=authorized_at,
        journal=journal,
    )


@dataclass(frozen=True)
class VendorEvent:
    """One entry of the vendor's append-only event log.

    ``at`` is the vendor's OWN clock (one process); ``kind`` is the
    closed vocabulary the grader reads: ``vendor_once`` /
    ``vendor_process_started`` / ``turn_started`` / ``vendor_edits`` /
    ``steer_poll`` / ``steer_consumed`` / ``vendor_edits_after_steer`` /
    ``steer_window_expired`` / ``turn_completed`` / ``turn_steered`` /
    ``turn_interrupted`` / ``vendor_process_exit``. ``details`` carries
    the evidence payload (command ids, touched paths, sources).
    """

    at: str
    kind: str
    details: dict[str, Any]


def read_vendor_events(raw_lines: Any) -> list[VendorEvent]:
    """Parse the vendor's JSONL event log (a path or an iterable of lines)."""
    if isinstance(raw_lines, (str, os.PathLike)):
        with open(raw_lines, encoding="utf-8") as handle:  # noqa: SIM115 - one read
            lines = handle.read().splitlines()
    else:
        lines = list(raw_lines)
    events: list[VendorEvent] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        details = {key: value for key, value in entry.items() if key not in ("at", "kind", "pid")}
        events.append(
            VendorEvent(
                at=str(entry.get("at", "")), kind=str(entry.get("kind", "")), details=details
            )
        )
    return events


@dataclass(frozen=True)
class EditSet:
    """One arm's observed file edits: the pre-turn base and the final bytes."""

    base: dict[str, str]
    files: dict[str, str]

    def as_document(self) -> dict[str, Any]:
        return {"base": dict(self.base), "files": dict(self.files)}


def edit_set_of(base: Mapping[str, str], files: Mapping[str, str]) -> EditSet:
    return EditSet(base=dict(base), files=dict(files))


def edits_differ(left: EditSet, right: EditSet) -> bool:
    """Whether two arms' edit sets are not byte-identical.

    Compared over the UNION of touched paths: a file only one arm wrote,
    or different final bytes anywhere, is a difference. The comparison
    is over OBSERVED EDITS (base vs final per arm), not the bases —
    the arms start from the same frozen task, and the grader says so by
    construction rather than by re-checking it.
    """
    paths = set(left.files) | set(right.files)
    return any(left.files.get(path) != right.files.get(path) for path in paths)


@dataclass(frozen=True)
class SteeringRun:
    """Everything the grader needs for ONE arm, already captured.

    ``provenance`` labels what backed the vendor:
    :data:`SCRIPTED_CAUSAL_PROVENANCE` (the reactive script) or
    ``live-model:<model>`` (the gateway arm — never fabricated). The
    counterfactual is a RUNNABLE arm's captured edits, not a guess.
    """

    arm: str  # "steered" | "counterfactual"
    provenance: str
    vendor_events: tuple[VendorEvent, ...]
    edits: EditSet
    command: SteeringCommandEvidence | None = None
    counterfactual_edits: EditSet | None = None

    def as_document(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "provenance": self.provenance,
            "command": (
                {
                    "command_id": self.command.command_id,
                    "kind": self.command.kind,
                    "text": self.command.text,
                    "status": self.command.status,
                    "received_at": self.command.received_at,
                    "authorized_at": self.command.authorized_at,
                }
                if self.command is not None
                else None
            ),
            "vendor_events": [
                {"at": event.at, "kind": event.kind, **event.details}
                for event in self.vendor_events
            ],
            "edits": self.edits.as_document(),
            "counterfactual_edits": (
                self.counterfactual_edits.as_document()
                if self.counterfactual_edits is not None
                else None
            ),
        }


@dataclass(frozen=True)
class CausalityArm:
    """One graded arm: a stable name, the verdict, the durable reason."""

    name: str
    ok: bool
    reason: str

    def as_document(self) -> dict[str, Any]:
        return {"arm": self.name, "ok": self.ok, "reason": self.reason}


@dataclass(frozen=True)
class CausalityGrade:
    """The three-arm verdict; ``causal`` only when every arm holds."""

    arms: tuple[CausalityArm, ...]
    provenance: str = ""

    @property
    def causal(self) -> bool:
        return bool(self.arms) and all(arm.ok for arm in self.arms)

    def arm(self, name: str) -> CausalityArm:
        for candidate in self.arms:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def as_document(self) -> dict[str, Any]:
        return {
            "causal": self.causal,
            "provenance": self.provenance,
            "arms": [arm.as_document() for arm in self.arms],
        }


def _arm(name: str, ok: bool, reason: str) -> CausalityArm:
    return CausalityArm(name=name, ok=ok, reason=reason)


def _iso_le(left: str, right: str) -> bool:
    """Chronological ``left <= right`` over ISO-8601 strings (any spelling).

    Both sides parse through :meth:`datetime.datetime.fromisoformat`
    (which handles ``Z`` suffixes and any fractional-second width), so a
    database clock's ``+00:00`` and the vendor clock's ``Z`` compare as
    INSTANTS, never as strings. Unparseable input is a failed compare —
    an unorderable pair is never silently ordered.
    """
    from datetime import datetime

    try:
        return datetime.fromisoformat(left) <= datetime.fromisoformat(right)
    except ValueError:
        return False


def _target_applied(target: SemanticTarget, edits: EditSet) -> bool:
    """Whether the edit set shows exactly the rename the target names."""
    base_content = edits.base.get(target.path)
    final_content = edits.files.get(target.path)
    if base_content is None or final_content is None:
        return False
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(target.old)}(?![A-Za-z0-9_])", base_content)
        is not None
        and re.search(rf"(?<![A-Za-z0-9_]){re.escape(target.new)}(?![A-Za-z0-9_])", final_content)
        is not None
        and re.search(rf"(?<![A-Za-z0-9_]){re.escape(target.old)}(?![A-Za-z0-9_])", final_content)
        is None
    )


def grade_causality(run: SteeringRun) -> CausalityGrade:
    """Grade the three causality arms over one captured steered run.

    Arm 1 — ``ack_precedes_edit``: the run carries the durable command;
    the vendor's log holds a ``steer_consumed`` event NAMING that
    command id (or carrying its exact text from the wire); the durable
    ACCEPTANCE (``received_at`` — the mailbox row's first journal hop,
    the operator's ACK) is not after the consumption (the lane's LATER
    ``authorized`` ladder hop is delivery machinery, not the ACK, and
    may legitimately trail a vendor that polled the row off the
    ``received`` rung); and at least one ``vendor_edits_after_steer``
    event follows the consumption IN THE SAME LOG (the subsequent edit
    the arm is about).

    Arm 2 — ``counterfactual_differs``: a captured counterfactual edit
    set exists and is not byte-identical to the steered arm's.

    Arm 3 — ``target_matched``: the instruction parses to a checkable
    transformation; the steered edit set shows exactly that
    transformation; and the counterfactual did NOT perform it on its
    own (the rename is the steer's effect, not the task's default).

    ``causal`` is the conjunction — nothing else.
    """
    # The grammar lives with the scenario (reference.reactive_vendor) so
    # the vendor's application and this grader's arm 3 share ONE parse —
    # the local-import pattern _iso_le set (the module stays stdlib-only
    # for the spawned arm).
    from forge.adaptive.reference.reactive_vendor import parse_instruction

    arms: list[CausalityArm] = []

    # -- arm 1: ordering ------------------------------------------------------
    command = run.command
    if command is None:
        arms.append(
            _arm(
                "ack_precedes_edit",
                False,
                "no steering command evidence on the run — nothing to order against",
            )
        )
    else:
        consumed_index = next(
            (
                index
                for index, event in enumerate(run.vendor_events)
                if event.kind == "steer_consumed"
                and (
                    event.details.get("command_id") == command.command_id
                    or str(event.details.get("text", "")) == command.text
                )
            ),
            None,
        )
        if consumed_index is None:
            arms.append(
                _arm(
                    "ack_precedes_edit",
                    False,
                    f"the vendor never logged steer_consumed for command "
                    f"{command.command_id!r} — consumption is unproven",
                )
            )
        else:
            consumed_at = str(run.vendor_events[consumed_index].at)
            ack_at = command.received_at
            ordered = _iso_le(ack_at, consumed_at) or not consumed_at
            subsequent = [
                event
                for event in run.vendor_events[consumed_index + 1 :]
                if event.kind == "vendor_edits_after_steer"
            ]
            if not ordered:
                arms.append(
                    _arm(
                        "ack_precedes_edit",
                        False,
                        f"the durable ACK ({ack_at}) is AFTER the vendor's consumption "
                        f"({consumed_at}) — the edit cannot have been caused by the command",
                    )
                )
            elif not subsequent:
                arms.append(
                    _arm(
                        "ack_precedes_edit",
                        False,
                        "no vendor edit event follows the steer consumption — there is "
                        "no subsequent behavior to grade",
                    )
                )
            else:
                arms.append(
                    _arm(
                        "ack_precedes_edit",
                        True,
                        f"command {command.command_id} acknowledged at {ack_at}, consumed "
                        f"mid-turn at {consumed_at}, then {len(subsequent)} subsequent "
                        "edit event(s) in the vendor's own log",
                    )
                )

    # -- arm 2: the counterfactual -------------------------------------------
    if run.counterfactual_edits is None:
        arms.append(
            _arm(
                "counterfactual_differs",
                False,
                "no captured counterfactual arm — a guessed counterfactual is not evidence",
            )
        )
    elif not edits_differ(run.edits, run.counterfactual_edits):
        arms.append(
            _arm(
                "counterfactual_differs",
                False,
                "the steered and unsteered arms produced IDENTICAL edits — the steer "
                "changed nothing",
            )
        )
    else:
        arms.append(
            _arm(
                "counterfactual_differs",
                True,
                "the same task without the steer produced a different edit set",
            )
        )

    # -- arm 3: the semantic target ------------------------------------------
    text = run.command.text if run.command is not None else ""
    target = parse_instruction(text)
    if target is None:
        arms.append(
            _arm(
                "target_matched",
                False,
                f"the instruction is not a checkable transformation: {text!r}",
            )
        )
    elif not _target_applied(target, run.edits):
        arms.append(
            _arm(
                "target_matched",
                False,
                f"the steered edits do not show '{target.old}' renamed to "
                f"'{target.new}' in {target.path}",
            )
        )
    elif run.counterfactual_edits is not None and _target_applied(target, run.counterfactual_edits):
        arms.append(
            _arm(
                "target_matched",
                False,
                "the UNSTEERED arm performed the same transformation — the edit is "
                "the task's default, not the steer's effect",
            )
        )
    else:
        arms.append(
            _arm(
                "target_matched",
                True,
                f"the edit shows exactly '{target.old}' renamed to '{target.new}' in "
                f"{target.path}, and the unsteered arm did not do it",
            )
        )

    return CausalityGrade(arms=tuple(arms), provenance=run.provenance)


if __name__ == "__main__":  # the lane spawns this file as CODEX_BINARY
    # The executable half moved to forge.adaptive.reference.reactive_vendor
    # (R37-19/#300) — stdlib-only and self-contained BY DESIGN. This spawn
    # runs under /usr/bin/env python3, where NO forge import is guaranteed
    # to resolve, so the reference module is loaded BY PATH and handed
    # argv exactly as the old inline vendor_main was (the behavior —
    # wire frames, steer window, event log — is unchanged).
    import importlib.util
    from pathlib import Path

    _vendor_path = Path(__file__).resolve().parent / "reference" / "reactive_vendor.py"
    _spec = importlib.util.spec_from_file_location("forge_reactive_vendor_spawn", _vendor_path)
    assert _spec is not None and _spec.loader is not None  # the sibling always exists
    _vendor = importlib.util.module_from_spec(_spec)
    # Registered BEFORE exec: the reference module's dataclasses resolve
    # their module globals through sys.modules on modern pythons.
    sys.modules[_spec.name] = _vendor
    _spec.loader.exec_module(_vendor)
    raise SystemExit(_vendor.vendor_main(sys.argv[1:]))
