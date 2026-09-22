"""NXT-26 — independent contract and DB/broker checks for a real candidate.

The lane that PRODUCED a candidate is the least credible witness for it:
the agent's own narrative, its meta claims and its diff are inputs to
check, never facts to adopt. This module is the checker that does not
trust the lane's own claims — two suites plus one typed report:

- :class:`ContractCheckSuite` — PURE structural checks of the PUBLISHED
  ``.forge/candidate.meta.json`` + ``.forge/candidate.diff`` contract (the
  one ``forge.runs.backends._collect_candidate`` and
  ``forge.lane_driver.write_artifacts`` define): schema exactness (required
  keys, exact types, no keys outside the published vocabulary — an
  invented key is a claim of authority the contract never granted),
  exit/terminal_reason coherence, usage honesty (unknown stays ``null``,
  never a zeroed dict; claimed completeness needs counters to back it),
  attempt-base shape/consistency against the PINNED base, diff
  parseability through the TRUSTED parser, path safety (no writes into
  the reserved ``.forge/`` namespace, no absolute or ``..`` paths) and
  exactly ONE representation per file (R08).
- :class:`DbIntegrationChecks` — the run-side truth cross-check over the
  durable tables: the meta's claimed attempt base against the run's
  FROZEN base (recomputed from ``flow_runs`` via the same
  :func:`forge.runs.candidate.attempt_base_for` the collection boundary
  uses — never from the artifact alone), the publication intent's commit
  against the run's recorded candidate head, branch exclusivity across
  runs (the ``mr_reservations`` shape: no OTHER candidate claims this
  run's branch), and control-queue sanity (``control_commands`` rows
  scoped to the run must not claim application their own durable audit
  contradicts; the table is read only when it exists — skip-clean).

:func:`run_independent_checks` folds both into an
:class:`IndependentCheckReport` — one :class:`CheckResult` per check with
a stable name, a ``pass``/``fail``/``skipped`` verdict and a one-line
evidence string. The report is EVIDENCE, deliberately never a verdict:
it exposes failures and skips for a reviewer to weigh, carries no
overall "verified" boolean, and :meth:`IndependentCheckReport.as_evidence`
produces the fragment a reviewer verdict record can cite. Humans and
reviewers decide; this module only measures. (Executing repository code
against real services — the disposable verifier lane of the full NXT-26
story — is out of scope here; this is the checking substrate that runs
before and beside it.)

R28-20 adds the COMPLETE-CandidateSet verification lane entry point:
:func:`verify_candidate_set` drives an INDEPENDENT pass over one FROZEN
:class:`~forge.adaptive.models.CandidateSet` — every member's
``candidate_oid`` probed for existence as a COMMIT on its repository
(through an injected lookup — the verifier's own clones or the provider
API, never the producing lane's word), the persisted
``tested_world_digest``/``applicability_digest`` RECOMPUTED from the
recorded facts (a set whose recorded world cannot be reproduced is
wrong whichever side lied), the ``environment_compose`` pins re-composed
(no unresolved service, no contradictory pin, a compose digest equal to
the persisted binding), the durable work-package record's active world
cross-checked, and the per-member PUBLISHED artifacts run through the
same :class:`ContractCheckSuite` above. The typed
:class:`VerificationReport` keeps per-member outcomes separate
(:class:`MemberVerification`) — one member's missing commit fails THAT
member without hiding the others; a digest mismatch fails the WHOLE set.
Like every report here it is evidence, never a verdict.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

# Registers control_commands/control_command_deliveries in Base.metadata
# (the durable mailbox tables this module reads).
from forge.adaptive.mailbox_db import ControlCommandRow
from forge.adaptive.models import CandidateSet
from forge.adaptive.verification_sets import environment_compose
from forge.adaptive.workpackage import (
    applicability_digest,
    read_workpackage_state,
    tested_world_digest,
)
from forge.durable.models import FlowRun, MRReservation, PublicationIntent
from forge.runs.candidate import CandidateError, attempt_base_for, parse_unified_diff

__all__ = [
    "DB_CHECK_NAMES",
    "CheckResult",
    "ContractCheckSuite",
    "DbIntegrationChecks",
    "IndependentCheckReport",
    "MemberVerification",
    "VerificationReport",
    "run_independent_checks",
    "verify_candidate_set",
]

#: One check's outcome. ``skipped`` is a RECORDED decision ("this check had
#: nothing to compare / its precondition failed"), never a silent absence.
CheckVerdict = Literal["pass", "fail", "skipped"]

#: The suite tag each result carries (contract vs run-side truth vs the
#: whole-set system verification of R28-20).
CheckSuite = Literal["contract", "db", "system"]

#: The meta keys every lane's ``write_artifacts`` writes (the required
#: contract). The attempt base may ride under the legacy GitHub-lane alias
#: ``attempt_base_oid`` — one base claim of SOME spelling must be present.
_REQUIRED_META_KEYS: tuple[str, ...] = (
    "driver",
    "model",
    "exit",
    "terminal_reason",
    "usage",
)
_BASE_META_KEYS: tuple[str, ...] = ("attempt_base", "attempt_base_oid")

#: The additive keys the readers know: audit/telemetry the published
#: contract tolerates. Anything OUTSIDE required + these is an invented
#: key — a claim of authority the contract never granted.
_KNOWN_OPTIONAL_META_KEYS: tuple[str, ...] = (
    "attempt_id",
    "summary",
    "error",
    "reply_excerpt",
    "steering_journal",
    "episode",
)

#: Meta keys that must be JSON strings when present.
_STRING_META_KEYS: tuple[str, ...] = (
    "attempt_base",
    "attempt_base_oid",
    "attempt_id",
    "driver",
    "model",
    "exit",
    "terminal_reason",
    "summary",
    "error",
    "reply_excerpt",
)

#: The usage receipt's token counters (the fields honesty is judged on).
_USAGE_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)

#: A full git commit OID: sha1 (40) or sha256 (64) lowercase hex.
_OID_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

#: The control-plane namespace inside the target repository: a candidate
#: that writes here can forge the NEXT attempt's own contract artifacts.
_RESERVED_NAMESPACE = ".forge"

#: Publication-intent states whose ``provider_object_id`` is a settled
#: commit (the effect provably landed — committed by us or adopted from a
#: previous attempt's probe).
_SETTLED_INTENT_STATUSES: tuple[str, ...] = ("committed", "adopted")

#: Mailbox statuses that CLAIM the control effect was applied.
_APPLIED_COMMAND_STATUSES: tuple[str, ...] = ("applied", "checkpointed")

#: The run-side check ids, in report order (``run_independent_checks``
#: cites them when no session factory is supplied).
DB_CHECK_NAMES: tuple[str, ...] = (
    "db.run.present",
    "db.attempt_base.matches_run",
    "db.publication_intent.head",
    "db.mr_reservation.branch_exclusive",
    "db.control_commands.coherence",
)


def _short(oid: str) -> str:
    """A 12-char preview for evidence lines (full values live in the DB)."""
    return oid[:12] if oid else "<none>"


def _base_claim(meta: Mapping[str, Any]) -> tuple[str, bool]:
    """(claimed base, alias-conflict?) from a meta's base spellings.

    ``attempt_base`` is the contract spelling; ``attempt_base_oid`` is
    the legacy GitHub-lane alias the reader still accepts. Two NON-EMPTY
    spellings that disagree are a conflict — one artifact claiming two
    bases is wrong whichever is true.
    """
    primary = meta.get("attempt_base")
    alias = meta.get("attempt_base_oid")
    primary_text = primary if isinstance(primary, str) else ""
    alias_text = alias if isinstance(alias, str) else ""
    conflict = bool(primary_text and alias_text and primary_text != alias_text)
    return primary_text or alias_text, conflict


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One named check's verdict and its single evidence line.

    ``check`` is a stable dotted id (``contract.meta.schema``,
    ``db.attempt_base.matches_run``, …) — evidence records cite ids, not
    prose, so a renamed check must be a new id.
    """

    check: str
    verdict: CheckVerdict
    evidence: str
    suite: CheckSuite


@dataclass(frozen=True)
class IndependentCheckReport:
    """The typed evidence record of one candidate's independent checks.

    Deliberately NOT a verdict: there is no aggregated pass/fail field,
    because "the independent result controls verified readiness" (NXT-26)
    is a decision a reviewer makes WEIGHING this evidence — a green
    aggregate here would let a boolean substitute for that judgement.
    The report exposes what was measured (:attr:`results`), what failed
    (:meth:`failures`) and what could not be measured (:meth:`skipped`),
    nothing more.
    """

    results: tuple[CheckResult, ...]

    def by_check(self, check: str) -> CheckResult | None:
        """The result named *check* (or ``None`` — an absent check is a
        caller error, not a pass)."""
        for result in self.results:
            if result.check == check:
                return result
        return None

    def failures(self) -> tuple[CheckResult, ...]:
        """The failed checks — the lines a reviewer must weigh first."""
        return tuple(result for result in self.results if result.verdict == "fail")

    def skipped(self) -> tuple[CheckResult, ...]:
        """The checks that could not run, each with its recorded reason."""
        return tuple(result for result in self.results if result.verdict == "skipped")

    def as_evidence(self) -> dict[str, Any]:
        """The evidence fragment for a reviewer verdict record.

        Shape follows the R02 evidence-fragment convention (the family
        ``flow_runs.evidence["verification"]`` uses): a producer-neutral
        dict a run/review record can store verbatim. ``role`` says
        ``evidence`` on purpose — consumers must never read this as a
        verdict.
        """
        return {
            "role": "evidence",
            "note": "independent checks feed reviewer evidence; they never substitute a verdict",
            "checks": [
                {
                    "check": result.check,
                    "suite": result.suite,
                    "verdict": result.verdict,
                    "evidence": result.evidence,
                }
                for result in self.results
            ],
            "failed": [result.check for result in self.failures()],
            "skipped": [result.check for result in self.skipped()],
        }

    def summary_line(self) -> str:
        """One log/evidence line: counts and failed check ids (not a verdict)."""
        passed = sum(1 for result in self.results if result.verdict == "pass")
        failed = [result.check for result in self.failures()]
        skipped = sum(1 for result in self.results if result.verdict == "skipped")
        parts = [
            f"{len(self.results)} checks: {passed} pass, {len(failed)} fail, {skipped} skipped"
        ]
        if failed:
            parts.append("failures: " + ", ".join(failed))
        return " — ".join(parts)


# ----------------------------------------------------------------------
# The contract suite (pure: the published artifact against the contract)
# ----------------------------------------------------------------------


class ContractCheckSuite:
    """Structural checks of one candidate's meta + diff, against the
    PUBLISHED contract only — no DB, no network, no lane code executed.

    The suite never decides whether the candidate is good; it decides
    whether the artifact is even SHAPED like an honest one. Everything
    here re-derives from the same contract the trusted collection
    boundary enforces, so a lane that ships a doctored meta or a
    path-escaping diff is caught beside — not instead of — that boundary.
    """

    def __init__(
        self,
        candidate_meta: Mapping[str, Any],
        candidate_diff_path: Path | str,
        *,
        expected_attempt_base: str | None = None,
    ) -> None:
        self._meta = dict(candidate_meta)
        self._diff_path = Path(candidate_diff_path)
        #: The PINNED base the caller trusts (what forge froze for this
        #: attempt). None → only the claim's shape is checked.
        self._expected_attempt_base = expected_attempt_base

    # -- entry ------------------------------------------------------------

    def run(self) -> tuple[CheckResult, ...]:
        """Every contract check, in a fixed evidence order."""
        return (
            self.check_meta_schema(),
            self.check_exit_coherence(),
            self.check_usage_honesty(),
            self.check_attempt_base(),
            self.check_diff_parseable(),
            self.check_diff_path_safety(),
            self.check_diff_single_representation(),
        )

    # -- helpers ------------------------------------------------------------

    def _base_claim(self) -> tuple[str, bool]:
        """The module-level base-claim extraction over this suite's meta."""
        return _base_claim(self._meta)

    # -- the checks ---------------------------------------------------------

    def check_meta_schema(self) -> CheckResult:
        """Required keys present, exact types, no invented keys.

        An extra key is not harmless telemetry at the top level: the
        meta is the lane's ONLY sanctioned voice, and a key like
        ``approved`` or ``verified`` smuggles a claim of authority the
        published contract never granted. Additive fields join the
        known vocabulary deliberately, in the contract, not by silence.
        """
        violations: list[str] = []
        for key in _REQUIRED_META_KEYS:
            if key not in self._meta:
                violations.append(f"missing required key {key!r}")
        if not any(key in self._meta for key in _BASE_META_KEYS):
            violations.append(
                "missing required key 'attempt_base' (nor the legacy 'attempt_base_oid' alias)"
            )
        for key in _STRING_META_KEYS:
            if key in self._meta and not isinstance(self._meta[key], str):
                violations.append(
                    f"key {key!r} must be a string, got {type(self._meta[key]).__name__}"
                )
        usage = self._meta.get("usage")
        if usage is not None and not isinstance(usage, dict):
            violations.append(f"key 'usage' must be an object or null, got {type(usage).__name__}")
        if "steering_journal" in self._meta and not isinstance(
            self._meta["steering_journal"], list
        ):
            violations.append("key 'steering_journal' must be a list")
        if "episode" in self._meta and not isinstance(self._meta["episode"], dict):
            violations.append("key 'episode' must be an object")
        known = set(_REQUIRED_META_KEYS) | set(_BASE_META_KEYS) | set(_KNOWN_OPTIONAL_META_KEYS)
        extras = sorted(set(self._meta) - known)
        if extras:
            violations.append(
                "keys outside the published contract (no authority granted): "
                + ", ".join(repr(key) for key in extras)
            )
        if violations:
            return CheckResult("contract.meta.schema", "fail", "; ".join(violations), "contract")
        return CheckResult(
            "contract.meta.schema",
            "pass",
            f"{len(self._meta)} keys, all required keys present with exact types",
            "contract",
        )

    def check_exit_coherence(self) -> CheckResult:
        """``exit`` and ``terminal_reason`` must tell ONE story.

        The lane's own classifier (:func:`forge.lane_driver.classify_result`)
        only writes ``completed`` with ``terminal_reason == "completed"``
        (an empty reason is tolerated for pre-0.2.118 stacks); every
        failure carries its reason. The inverse shapes — success with an
        aborted reason, failure with no reason, an exit outside the
        vocabulary — are claims the contract does not allow.
        """
        exit_value = self._meta.get("exit")
        reason = self._meta.get("terminal_reason")
        if not isinstance(exit_value, str) or not isinstance(reason, str):
            return CheckResult(
                "contract.meta.exit_coherence",
                "fail",
                "exit/terminal_reason are not both strings (schema check reports the shape)",
                "contract",
            )
        if exit_value not in ("completed", "failed"):
            return CheckResult(
                "contract.meta.exit_coherence",
                "fail",
                f"exit {exit_value!r} is outside the published vocabulary (completed|failed)",
                "contract",
            )
        if exit_value == "completed" and reason not in ("", "completed"):
            return CheckResult(
                "contract.meta.exit_coherence",
                "fail",
                f"exit claims completed but terminal_reason says {reason!r}",
                "contract",
            )
        if exit_value == "failed" and not reason.strip():
            return CheckResult(
                "contract.meta.exit_coherence",
                "fail",
                "exit claims failed without a terminal_reason",
                "contract",
            )
        return CheckResult(
            "contract.meta.exit_coherence",
            "pass",
            f"exit={exit_value!r} corroborated by terminal_reason={reason!r}",
            "contract",
        )

    def check_usage_honesty(self) -> CheckResult:
        """Unknown usage stays ``null`` — never zeroed, never invented.

        The receipt contract (F22 lite / R23): absent or ``null`` is the
        HONEST unknown; a present receipt must carry real counters. A
        dict of explicit zeros claiming completeness is the zero-lie (a
        model turn that consumed nothing did not happen), and a claimed
        ``exact``/``aggregate`` completeness with no counters at all
        claims precision the receipt cannot support (the trusted parser
        would silently degrade it to ``unknown`` — this check surfaces
        the lie instead of absorbing it).
        """
        usage = self._meta.get("usage")
        if usage is None:
            return CheckResult(
                "contract.meta.usage_honesty",
                "pass",
                "usage is null — unknown stays unknown (never zeroed)",
                "contract",
            )
        if not isinstance(usage, dict):
            return CheckResult(
                "contract.meta.usage_honesty",
                "fail",
                f"usage must be an object or null, got {type(usage).__name__}",
                "contract",
            )
        recognized = [key for key in _USAGE_TOKEN_KEYS if key in usage]
        if not any(key in usage for key in (*_USAGE_TOKEN_KEYS, "completeness", "source")):
            return CheckResult(
                "contract.meta.usage_honesty",
                "fail",
                "present-but-empty receipt object: unknown must be null, never a zeroed dict",
                "contract",
            )
        for key in _USAGE_TOKEN_KEYS:
            if key not in usage:
                continue
            value = usage[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return CheckResult(
                    "contract.meta.usage_honesty",
                    "fail",
                    f"counter {key}={value!r} is not a non-negative integer",
                    "contract",
                )
        completeness = usage.get("completeness")
        claimed = completeness in ("exact", "aggregate")
        if claimed and not recognized:
            return CheckResult(
                "contract.meta.usage_honesty",
                "fail",
                f"completeness={completeness!r} claimed with no token counters to back it",
                "contract",
            )
        if claimed and recognized and all(usage[key] == 0 for key in recognized):
            return CheckResult(
                "contract.meta.usage_honesty",
                "fail",
                f"zeroed receipt (all of {recognized} are 0) claims completeness={completeness!r}",
                "contract",
            )
        counters = ", ".join(f"{key}={usage[key]}" for key in recognized) or "no counters"
        return CheckResult(
            "contract.meta.usage_honesty",
            "pass",
            f"receipt present: {counters}, completeness={completeness!r}",
            "contract",
        )

    def check_attempt_base(self) -> CheckResult:
        """The base claim must be a full OID — and THE pinned base when
        the caller supplies one.

        Pure shape here; the run-side recomputation
        (``db.attempt_base.matches_run``) is the deeper cross-check
        against durable state. Two conflicting spellings of the base in
        one meta fail whichever is true.
        """
        check = "contract.attempt_base.consistency"
        claimed, conflict = self._base_claim()
        if conflict:
            return CheckResult(
                check,
                "fail",
                "attempt_base and attempt_base_oid both present and disagreeing "
                f"({_short(str(self._meta.get('attempt_base')))} vs "
                f"{_short(str(self._meta.get('attempt_base_oid')))})",
                "contract",
            )
        if not claimed:
            return CheckResult(check, "fail", "no attempt-base claim in the meta", "contract")
        if not _OID_RE.fullmatch(claimed):
            return CheckResult(
                check,
                "fail",
                f"attempt base {claimed!r} is not a full commit OID (40/64 lowercase hex)",
                "contract",
            )
        if self._expected_attempt_base is not None and claimed != self._expected_attempt_base:
            return CheckResult(
                check,
                "fail",
                f"claimed base {_short(claimed)}… does not match the pinned base "
                f"{_short(self._expected_attempt_base)}…",
                "contract",
            )
        pinned = (
            f", matches the pinned base {_short(self._expected_attempt_base)}…"
            if self._expected_attempt_base is not None
            else " (no pinned base supplied: shape only)"
        )
        return CheckResult(
            check, "pass", f"attempt base {_short(claimed)}… is a full OID{pinned}", "contract"
        )

    def _read_diff(self) -> str | None:
        """The diff text, or None when the artifact itself is missing."""
        try:
            return self._diff_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return None

    def _parsed_entries(self) -> tuple[tuple[str, ...] | None, str]:
        """(entry paths, evidence note) — None paths when unparseable."""
        text = self._read_diff()
        if text is None:
            return None, f"diff artifact {self._diff_path} is missing/unreadable"
        claimed, _conflict = self._base_claim()
        try:
            bundle = parse_unified_diff(text, attempt_base_oid=claimed)
        except CandidateError as exc:
            return None, f"{exc.reason}: {exc}"
        return tuple(bundle.paths), "parsed through the trusted parser"

    def check_diff_parseable(self) -> CheckResult:
        """The diff must survive the TRUSTED parser — binary deltas,
        renames, corrupt hunks and count mismatches are rejections, not
        noise to wave through."""
        paths, note = self._parsed_entries()
        if paths is None:
            return CheckResult("contract.diff.parseable", "fail", note, "contract")
        return CheckResult(
            "contract.diff.parseable",
            "pass",
            f"{len(paths)} file entries, {note}",
            "contract",
        )

    def check_diff_path_safety(self) -> CheckResult:
        """Every entry path stays inside the repository's working tree.

        The reserved ``.forge/`` namespace is where the contract's own
        artifacts live: a candidate that writes there can forge the next
        attempt's meta/usage receipts. Absolute paths, ``..``
        traversals and Windows-drive spellings never name a file the
        publisher is authorized to write.
        """
        paths, note = self._parsed_entries()
        if paths is None:
            return CheckResult(
                "contract.diff.path_safety", "skipped", f"diff not parseable ({note})", "contract"
            )
        violations: list[str] = []
        for path in paths:
            violations.extend(_path_violations(path))
        if violations:
            return CheckResult(
                "contract.diff.path_safety", "fail", "; ".join(violations), "contract"
            )
        return CheckResult(
            "contract.diff.path_safety",
            "pass",
            f"all {len(paths)} entry paths are safe (relative, no .., outside .forge/)",
            "contract",
        )

    def check_diff_single_representation(self) -> CheckResult:
        """Exactly ONE representation per file (R08): a diff that touches
        the same path through two entries is ambiguous by construction —
        whichever the applier picked, the candidate meant something else."""
        paths, note = self._parsed_entries()
        if paths is None:
            return CheckResult(
                "contract.diff.single_representation",
                "skipped",
                f"diff not parseable ({note})",
                "contract",
            )
        seen: set[str] = set()
        duplicates: set[str] = set()
        for path in paths:
            if path in seen:
                duplicates.add(path)
            seen.add(path)
        if duplicates:
            listing = ", ".join(sorted(duplicates))
            return CheckResult(
                "contract.diff.single_representation",
                "fail",
                f"multiple entries for: {listing}",
                "contract",
            )
        return CheckResult(
            "contract.diff.single_representation",
            "pass",
            f"{len(paths)} entries, one representation per file",
            "contract",
        )


def _path_violations(path: str) -> list[str]:
    """The path-safety violations of ONE diff entry path."""
    if not path or path in (".", "/"):
        return [f"unusable entry path {path!r}"]
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("/"):
        return [f"absolute entry path {path!r}"]
    parts = pure.parts
    if len(parts[0]) == 2 and parts[0][1] == ":":
        return [f"windows-absolute entry path {path!r}"]
    if ".." in parts:
        return [f"path traversal in entry path {path!r}"]
    if parts[0] == _RESERVED_NAMESPACE:
        return [f"entry path {path!r} writes into the reserved {_RESERVED_NAMESPACE}/ namespace"]
    return []


# ----------------------------------------------------------------------
# The DB/broker suite (run-side truth the lane cannot rewrite)
# ----------------------------------------------------------------------


class DbIntegrationChecks:
    """Cross-check one candidate's claims against the controller's DB.

    The lane authored the meta; the controller authored the run rows.
    These checks re-derive the run-side truth (frozen attempt base,
    recorded publication head, branch reservations, control-queue
    audit) and compare — a candidate whose story diverges from the
    durable state fails HERE even when its artifact is perfectly
    well-formed. Tables that a deployment does not carry are skipped
    CLEAN (a recorded skip with a reason, never a silent pass).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], run_id: str) -> None:
        self._session_factory = session_factory
        self._run_id = run_id

    async def run(self, *, claimed_attempt_base: str = "") -> tuple[CheckResult, ...]:
        """Every run-side check, in a fixed evidence order."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, self._run_id)
            has_control = await self._has_table(session, "control_commands")
            return (
                self._check_run_present(run),
                self._check_attempt_base(run, claimed_attempt_base),
                await self._check_publication_intent_head(session, run),
                await self._check_branch_exclusive(session, run),
                await self._check_control_commands(session, has_control),
            )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    async def _has_table(session: AsyncSession, name: str) -> bool:
        """Whether *name* exists in this database (skip-clean probe)."""
        connection: AsyncConnection = await session.connection()
        return bool(await connection.run_sync(lambda sync: sa_inspect(sync).has_table(name)))

    # -- the checks ---------------------------------------------------------

    def _check_run_present(self, run: FlowRun | None) -> CheckResult:
        if run is None:
            return CheckResult(
                "db.run.present", "fail", f"no flow_runs row for run {self._run_id!r}", "db"
            )
        return CheckResult(
            "db.run.present",
            "pass",
            f"run {self._run_id!r} status={run.status!r} cycle={run.commit_cycle}",
            "db",
        )

    def _check_attempt_base(self, run: FlowRun | None, claimed: str) -> CheckResult:
        """The meta's base against the run's FROZEN base, recomputed
        from durable state — the exact re-check ``_collect_candidate``
        performs, kept independent here so it cannot be retired by the
        lane's own code path."""
        check = "db.attempt_base.matches_run"
        if run is None:
            return CheckResult(check, "skipped", "run row absent", "db")
        if not claimed:
            return CheckResult(
                check,
                "skipped",
                "meta carries no attempt-base claim (the contract check reports it)",
                "db",
            )
        frozen = attempt_base_for(run)
        if claimed != frozen:
            return CheckResult(
                check,
                "fail",
                f"claimed base {_short(claimed)}… but the run's frozen base is "
                f"{_short(frozen)}… (cycle {run.commit_cycle})",
                "db",
            )
        return CheckResult(
            check,
            "pass",
            f"claimed base {_short(claimed)}… equals the run's frozen base "
            f"(cycle {run.commit_cycle})",
            "db",
        )

    async def _check_publication_intent_head(
        self, session: AsyncSession, run: FlowRun | None
    ) -> CheckResult:
        """The settled publication intent's commit == the candidate's
        published head (``flow_runs.candidate_shas[-1]``).

        Both sides are controller-owned rows: a candidate whose recorded
        head is not the commit its own publication intent settled on is
        a run whose story disagrees with itself.
        """
        check = "db.publication_intent.head"
        if run is None:
            return CheckResult(check, "skipped", "run row absent", "db")
        candidate_shas = list(run.candidate_shas or [])
        if not candidate_shas:
            return CheckResult(
                check, "skipped", "run records no candidate head (nothing published yet)", "db"
            )
        intents = (
            (
                await session.execute(
                    select(PublicationIntent)
                    .where(
                        PublicationIntent.run_id == self._run_id,
                        PublicationIntent.operation == "commit",
                    )
                    .order_by(PublicationIntent.created_at.desc(), PublicationIntent.id.desc())
                )
            )
            .scalars()
            .all()
        )
        settled = next(
            (intent for intent in intents if intent.status in _SETTLED_INTENT_STATUSES), None
        )
        if settled is None:
            statuses = ", ".join(sorted({intent.status for intent in intents})) or "none"
            return CheckResult(
                check,
                "skipped",
                f"{len(intents)} commit intent(s), none settled (statuses: {statuses})",
                "db",
            )
        published = settled.provider_object_id or ""
        head = str(candidate_shas[-1])
        if published != head:
            return CheckResult(
                check,
                "fail",
                f"settled intent ({settled.status}) commit {_short(published)}… but the run's "
                f"recorded candidate head is {_short(head)}…",
                "db",
            )
        return CheckResult(
            check,
            "pass",
            f"settled intent ({settled.status}) commit {_short(published)}… equals the "
            f"recorded candidate head",
            "db",
        )

    async def _check_branch_exclusive(
        self, session: AsyncSession, run: FlowRun | None
    ) -> CheckResult:
        """No OTHER run's reservation claims this run's branch.

        ``mr_reservations`` enforces one reservation per (run, branch);
        cross-RUN exclusivity is the branch-naming discipline this check
        verifies against the rows — two candidates publishing one branch
        have no single owner for its publication intent.
        """
        check = "db.mr_reservation.branch_exclusive"
        if run is None:
            return CheckResult(check, "skipped", "run row absent", "db")
        own = (
            (
                await session.execute(
                    select(MRReservation).where(MRReservation.flow_run_id == self._run_id)
                )
            )
            .scalars()
            .all()
        )
        if not own:
            return CheckResult(
                check, "skipped", "run holds no MR reservations (no branch claim to verify)", "db"
            )
        branches = sorted({reservation.branch for reservation in own})
        others = (
            (
                await session.execute(
                    select(MRReservation).where(
                        MRReservation.branch.in_(branches),
                        MRReservation.flow_run_id != self._run_id,
                        MRReservation.status.in_(("open", "confirmed")),
                    )
                )
            )
            .scalars()
            .all()
        )
        if others:
            claims = ", ".join(
                sorted(
                    {
                        f"run {other.flow_run_id!r} on {other.branch!r} ({other.status})"
                        for other in others
                    }
                )
            )
            return CheckResult(
                check,
                "fail",
                f"another candidate claims this run's branch: {claims}",
                "db",
            )
        return CheckResult(
            check,
            "pass",
            f"{len(branches)} reserved branch(es) ({', '.join(branches)}) claimed by no other run",
            "db",
        )

    async def _check_control_commands(
        self, session: AsyncSession, has_control: bool
    ) -> CheckResult:
        """No run-scoped control command claims success its row contradicts.

        The durable ladder (NXT-12) books ``applied`` only after the
        vendor rung and stamps ``applied_at`` with it; the journal is
        the append-only audit of every transition. A row whose status
        says applied/checkpointed while ``applied_at`` is NULL, or whose
        journal's last transition is not the row's status, is a claim of
        success the database itself disagrees with — exactly the
        "unconsumed command claiming success" an independent check must
        surface. Without the table the check skips CLEAN (recorded, with
        its reason).
        """
        check = "db.control_commands.coherence"
        if not has_control:
            return CheckResult(
                check,
                "skipped",
                "control_commands table not present in this database (skip-clean)",
                "db",
            )
        rows = (
            (
                await session.execute(
                    select(ControlCommandRow).where(
                        or_(
                            ControlCommandRow.run_id == self._run_id,
                            ControlCommandRow.work_id == self._run_id,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        violations: list[str] = []
        for row in rows:
            if row.status in _APPLIED_COMMAND_STATUSES and row.applied_at is None:
                violations.append(f"command {row.id!r} claims {row.status} but applied_at is NULL")
            journal = list(row.journal or [])
            last_to = str(journal[-1].get("to", "")) if journal else ""
            if not journal or last_to != row.status:
                violations.append(
                    f"command {row.id!r} status {row.status!r} is not corroborated by its "
                    f"append-only journal (last transition: {last_to or 'none'})"
                )
        if violations:
            return CheckResult(check, "fail", "; ".join(violations), "db")
        return CheckResult(
            check,
            "pass",
            f"{len(rows)} run-scoped control command(s), every claim corroborated by its row",
            "db",
        )


# ----------------------------------------------------------------------
# The one entry point
# ----------------------------------------------------------------------


async def run_independent_checks(
    candidate_meta: Mapping[str, Any],
    candidate_diff_path: Path | str,
    run_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    expected_attempt_base: str | None = None,
) -> IndependentCheckReport:
    """Run both suites over one REAL candidate and fold the typed report.

    The contract suite always runs (it needs nothing but the published
    artifacts). The DB suite runs when a *session_factory* is supplied;
    without one every db check is a RECORDED skip — "run-side truth not
    cross-checked" is evidence a reviewer must see, not a hole to paper
    over. The result never substitutes a verdict (see
    :class:`IndependentCheckReport`).
    """
    contract = ContractCheckSuite(
        candidate_meta,
        candidate_diff_path,
        expected_attempt_base=expected_attempt_base,
    )
    results = list(contract.run())
    if session_factory is None:
        results.extend(
            CheckResult(
                name,
                "skipped",
                "no session factory supplied — run-side truth not cross-checked",
                "db",
            )
            for name in DB_CHECK_NAMES
        )
    else:
        claimed, _conflict = _base_claim(candidate_meta)
        db = DbIntegrationChecks(session_factory, run_id)
        results.extend(await db.run(claimed_attempt_base=claimed))
    return IndependentCheckReport(results=tuple(results))


# ----------------------------------------------------------------------
# R28-20: independent verification of one COMPLETE CandidateSet
# ----------------------------------------------------------------------

#: The commit-existence seam: does *candidate_oid* exist as a COMMIT on
#: *repository_id*? The verifier lane injects its own implementation (a
#: local clone's ``git cat-file -e``, a provider API call) — the producing
#: lane's claims are the thing being checked, so they cannot also be the
#: lookup. ``None`` (no seam) records a skip, never a pass.
CommitLookup = Callable[[str, str], Awaitable[bool]]

#: One member's published artifacts for the contract suite:
#: ``repository_id -> (candidate meta, candidate.diff path)``.
CandidateArtifacts = Mapping[str, tuple[Mapping[str, Any], Path | str]]

#: The per-member check ids (in evidence order) — uniform dotted names;
#: :class:`MemberVerification` carries WHICH member each outcome belongs to.
MEMBER_COMMIT_CHECK = "candidateset.member.commit_present"

#: The set-level check ids (in evidence order).
SET_CHECK_NAMES: tuple[str, ...] = (
    "candidateset.world_digest.recomputed",
    "candidateset.applicability_digest.recomputed",
    "candidateset.environment_compose.consistent",
    "candidateset.db.recorded_world",
)


@dataclass(frozen=True)
class MemberVerification:
    """ONE member's independent outcome — typed, never a merged blob.

    ``commit_present`` answers "does the recorded candidate commit exist
    on the repository" (a fail here fails THIS member, not the set);
    ``contract`` holds the member's published-artifact check results (or
    the recorded skip when the verifier was given no artifact for it).
    """

    repository_id: str
    role: str
    candidate_oid: str
    image_digest: str
    commit_present: CheckResult
    contract: tuple[CheckResult, ...]


@dataclass(frozen=True)
class VerificationReport:
    """The typed evidence record of one COMPLETE CandidateSet verification.

    Same honesty rule as :class:`IndependentCheckReport`: no aggregated
    verdict exists here — a digest mismatch fails the WHOLE set (a world
    that cannot be reproduced says nothing about any of its members),
    while a missing commit or a failed contract check fails exactly its
    member. Reviewers weigh :meth:`failures` and :meth:`skipped`; nothing
    in this report decides for them.
    """

    work_id: str
    plan_revision: int
    tested_world_digest: str | None
    members: tuple[MemberVerification, ...]
    set_checks: tuple[CheckResult, ...]

    def results(self) -> tuple[CheckResult, ...]:
        """Every check result, set-level first, then per member (commit,
        then contract) — the flat evidence order."""
        flattened = list(self.set_checks)
        for member in self.members:
            flattened.append(member.commit_present)
            flattened.extend(member.contract)
        return tuple(flattened)

    def failures(self) -> tuple[CheckResult, ...]:
        """The failed checks — a reviewer's first read."""
        return tuple(result for result in self.results() if result.verdict == "fail")

    def skipped(self) -> tuple[CheckResult, ...]:
        """The checks that could not run, each with its recorded reason."""
        return tuple(result for result in self.results() if result.verdict == "skipped")

    def failed_members(self) -> tuple[str, ...]:
        """The repositories whose OWN verification failed (commit or contract)."""
        failed: list[str] = []
        for member in self.members:
            results = (member.commit_present, *member.contract)
            if any(result.verdict == "fail" for result in results):
                failed.append(member.repository_id)
        return tuple(failed)

    def as_evidence(self) -> dict[str, Any]:
        """The evidence fragment for a reviewer verdict record (R02 shape)."""
        return {
            "role": "evidence",
            "note": "candidate-set verification feeds reviewer evidence; it never substitutes a verdict",
            "work_id": self.work_id,
            "plan_revision": self.plan_revision,
            "tested_world_digest": self.tested_world_digest,
            "set_checks": [
                {"check": result.check, "verdict": result.verdict, "evidence": result.evidence}
                for result in self.set_checks
            ],
            "members": [
                {
                    "repository_id": member.repository_id,
                    "role": member.role,
                    "candidate_oid": member.candidate_oid,
                    "commit_present": {
                        "check": member.commit_present.check,
                        "verdict": member.commit_present.verdict,
                        "evidence": member.commit_present.evidence,
                    },
                    "contract": [
                        {
                            "check": result.check,
                            "verdict": result.verdict,
                            "evidence": result.evidence,
                        }
                        for result in member.contract
                    ],
                }
                for member in self.members
            ],
            "failed": [result.check for result in self.failures()],
            "skipped": [result.check for result in self.skipped()],
            "failed_members": list(self.failed_members()),
        }

    def summary_line(self) -> str:
        """One log/evidence line: counts, failed checks, failed members."""
        results = self.results()
        failed = [result.check for result in self.failures()]
        skipped = len(self.skipped())
        parts = [
            f"{len(results)} checks: {len(results) - len(failed) - skipped} pass,"
            f" {len(failed)} fail, {skipped} skipped"
        ]
        if failed:
            parts.append("failures: " + ", ".join(failed))
        if self.failed_members():
            parts.append("failed members: " + ", ".join(self.failed_members()))
        return " — ".join(parts)


async def _check_member_commit(member: Any, commit_lookup: CommitLookup | None) -> CheckResult:
    """Probe ONE member's candidate_oid for existence as a commit.

    No seam → a RECORDED skip (the verifier without repository access
    measures nothing and must not pretend otherwise). A lookup that
    raises propagates: an unreachable repository is an operator problem,
    not a member verdict — and never a silent pass.
    """
    if commit_lookup is None:
        return CheckResult(
            MEMBER_COMMIT_CHECK,
            "skipped",
            "no commit lookup supplied — repository existence not probed",
            "system",
        )
    present = await commit_lookup(member.repository_id, member.candidate_oid)
    if not present:
        return CheckResult(
            MEMBER_COMMIT_CHECK,
            "fail",
            f"candidate {_short(member.candidate_oid)}… does not exist as a commit on"
            f" repository {member.repository_id!r}",
            "system",
        )
    return CheckResult(
        MEMBER_COMMIT_CHECK,
        "pass",
        f"candidate {_short(member.candidate_oid)}… exists as a commit on"
        f" repository {member.repository_id!r}",
        "system",
    )


def _member_contract_results(
    member: Any, artifacts: CandidateArtifacts | None
) -> tuple[CheckResult, ...]:
    """The member's published artifacts through the contract suite.

    The suite pins the member's OWN base (``member.base_oid`` — the diff
    provenance the frozen set records). No artifact supplied → ONE
    recorded skip, not seven pretend runs and not a pass.
    """
    if artifacts is None or member.repository_id not in artifacts:
        return (
            CheckResult(
                "contract.artifact.present",
                "skipped",
                f"no published candidate artifact supplied for {member.repository_id!r}",
                "system",
            ),
        )
    meta, diff_path = artifacts[member.repository_id]
    return ContractCheckSuite(meta, diff_path, expected_attempt_base=member.base_oid).run()


def _check_world_digest(candidate_set: CandidateSet) -> CheckResult:
    """The persisted tested-world digest must be RECOMPUTABLE from the facts.

    The set claims a frozen identity; this recomputes it from the
    recorded members, pins and policy refs with the same canonical
    function the freeze used. A mismatch fails the WHOLE set: whichever
    side lied, nothing downstream may bind to that digest. An unfrozen
    set skips — there is no recorded binding to reproduce.
    """
    check = "candidateset.world_digest.recomputed"
    persisted = candidate_set.tested_world_digest
    if persisted is None:
        return CheckResult(
            check,
            "skipped",
            "no persisted tested_world_digest — freeze_verified_world first;"
            " a binding must be recorded, not recomputed",
            "system",
        )
    recomputed = tested_world_digest(
        candidate_set,
        environment=dict(candidate_set.environment_pins),
        policy_refs=candidate_set.policy_refs,
    )
    if recomputed != persisted:
        return CheckResult(
            check,
            "fail",
            f"recorded facts recompute to {_short(recomputed)}…, not the persisted"
            f" {_short(persisted)}… — the frozen world identity does not reproduce;"
            " the whole set fails",
            "system",
        )
    return CheckResult(
        check,
        "pass",
        f"recorded facts reproduce the persisted world digest {_short(persisted)}…",
        "system",
    )


def _check_applicability_digest(candidate_set: CandidateSet) -> CheckResult:
    """Same reproduction test for the narrower applicability fingerprint."""
    check = "candidateset.applicability_digest.recomputed"
    persisted = candidate_set.applicability_digest
    if persisted is None:
        return CheckResult(
            check, "skipped", "no persisted applicability_digest (unfrozen set)", "system"
        )
    recomputed = applicability_digest(
        candidate_set,
        environment=dict(candidate_set.environment_pins),
        policy_refs=candidate_set.policy_refs,
    )
    if recomputed != persisted:
        return CheckResult(
            check,
            "fail",
            f"applicability recompute {_short(recomputed)}… != persisted"
            f" {_short(persisted)}… — the whole set fails",
            "system",
        )
    return CheckResult(
        check,
        "pass",
        f"applicability fingerprint reproduces {_short(persisted)}…",
        "system",
    )


def _check_environment_compose(candidate_set: CandidateSet) -> CheckResult:
    """The compose pins must be CONSISTENT with the frozen world.

    Re-composes the integration environment over every member and every
    pinned external service: a contradictory pin (two claimed artifacts
    for one launched thing) raises and fails here; a service that
    resolves to NO exact artifact composes ``unresolved`` and fails here
    — never "run it as whatever the tag points at today". On a frozen
    set the compose's own world digest must equal the persisted binding.
    """
    check = "candidateset.environment_compose.consistent"
    services = sorted(
        {member.repository_id for member in candidate_set.members}
        | set(dict(candidate_set.environment_pins))
    )
    try:
        composed = environment_compose(candidate_set, services)
    except ValueError as exc:
        return CheckResult(check, "fail", f"compose refuses the recorded pins: {exc}", "system")
    unresolved = sorted(
        str(entry.get("service"))
        for entry in composed.get("services", [])
        if entry.get("unresolved") or entry.get("artifact_digest") is None
    )
    if unresolved:
        return CheckResult(
            check,
            "fail",
            f"services without a recorded exact artifact (refusing to run a mutable"
            f" tag): {', '.join(unresolved)}",
            "system",
        )
    if candidate_set.tested_world_digest is not None:
        compose_world = str(composed.get("tested_world_digest") or "")
        if compose_world != candidate_set.tested_world_digest:
            return CheckResult(
                check,
                "fail",
                f"compose binds to {_short(compose_world)}… but the set is frozen to"
                f" {_short(candidate_set.tested_world_digest)}…",
                "system",
            )
        binding = f", compose digest equals the persisted binding {_short(compose_world)}…"
    else:
        binding = " (unfrozen set: consistency only)"
    return CheckResult(
        check,
        "pass",
        f"{len(services)} service(s) pinned to exact artifacts{binding}",
        "system",
    )


async def _check_recorded_world(
    session_factory: async_sessionmaker[AsyncSession], candidate_set: CandidateSet
) -> CheckResult:
    """The durable work-package record must carry the SAME active world.

    The coordinator persists the package's active ``tested_world_digest``
    in the parent run's evidence and rejects outcomes from any other
    world; a set whose digest disagrees with that record verifies one
    world while the package executes another.
    """
    from forge.adaptive.workpackage import WorkPackageStateError

    check = "candidateset.db.recorded_world"
    try:
        state = await read_workpackage_state(session_factory, candidate_set.work_id)
    except WorkPackageStateError as exc:
        return CheckResult(
            check,
            "fail",
            f"no durable coordination record for the set's work: {exc}",
            "system",
        )
    if state is None:
        return CheckResult(
            check,
            "skipped",
            f"run {candidate_set.work_id!r} coordinates no work package (no recorded active world)",
            "system",
        )
    recorded = state.get("tested_world_digest")
    if recorded is None:
        return CheckResult(
            check,
            "skipped",
            "the work package carries no active tested_world_digest",
            "system",
        )
    if candidate_set.tested_world_digest is None:
        return CheckResult(
            check,
            "skipped",
            "the candidate set is unfrozen (nothing persisted to compare)",
            "system",
        )
    if str(recorded) != candidate_set.tested_world_digest:
        return CheckResult(
            check,
            "fail",
            f"package's active world is {_short(str(recorded))}… but the set froze"
            f" {_short(candidate_set.tested_world_digest)}…",
            "system",
        )
    return CheckResult(
        check,
        "pass",
        f"package's active world equals the set's frozen digest"
        f" {_short(candidate_set.tested_world_digest)}…",
        "system",
    )


async def verify_candidate_set(
    candidate_set: CandidateSet,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    commit_lookup: CommitLookup | None = None,
    candidate_artifacts: CandidateArtifacts | None = None,
) -> VerificationReport:
    """Independently verify ONE complete (frozen) CandidateSet (R28-20).

    This is the separate verification pass the review demands — NOT the
    producing lane's claim re-stated. Four independent angles, folded
    into one :class:`VerificationReport` with typed per-member outcomes:

    (a) every member's ``candidate_oid`` probed for existence as a COMMIT
        on its repository, through the injected *commit_lookup* (skip
        recorded without one);
    (b) the persisted ``tested_world_digest`` AND ``applicability_digest``
        recomputed from the recorded facts — a mismatch fails the WHOLE
        set;
    (c) the ``environment_compose`` pins re-composed: contradictory pins
        refused, unresolved services named, the compose digest bound to
        the persisted world;
    (d) the per-member PUBLISHED artifacts (``candidate.meta.json`` +
        ``candidate.diff``) through :class:`ContractCheckSuite`, pinned to
        each member's own ``base_oid`` — plus the durable work-package
        record's active world cross-checked against the set.

    Like every report in this module the result is evidence: it never
    substitutes a reviewer's verdict.
    """
    ordered = sorted(candidate_set.members, key=lambda member: member.repository_id)
    verified: list[MemberVerification] = []
    for member in ordered:
        verified.append(
            MemberVerification(
                repository_id=member.repository_id,
                role=member.role,
                candidate_oid=member.candidate_oid,
                image_digest=member.image_digest,
                commit_present=await _check_member_commit(member, commit_lookup),
                contract=_member_contract_results(member, candidate_artifacts),
            )
        )
    set_checks = (
        _check_world_digest(candidate_set),
        _check_applicability_digest(candidate_set),
        _check_environment_compose(candidate_set),
        await _check_recorded_world(session_factory, candidate_set),
    )
    return VerificationReport(
        work_id=candidate_set.work_id,
        plan_revision=candidate_set.plan_revision,
        tested_world_digest=candidate_set.tested_world_digest,
        members=tuple(verified),
        set_checks=set_checks,
    )
