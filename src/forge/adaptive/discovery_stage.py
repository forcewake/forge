"""The durable discovery stage for the normal /implement path (NXT-05).

The review's finding: ``LLMPlanner.plan()`` plans from the issue text
alone (12k input cap), while :class:`~forge.adaptive.discovery.DiscoveryRun`
and the bounded :class:`~forge.adaptive.discovery_tools.SnapshotToolbox`
exist but are not reached by a real ``/implement`` run. This module is
the integration slice that connects them:

- :func:`maybe_run_discovery` — the seam the production path splices in
  between run start and planning (see ``docs/adaptive/discovery-splice.md``
  for the exact patch). Gated by ``FORGE_DISCOVERY_ENABLED`` and OFF by
  default in this slice — an honest rollout, never a silent flip.
- :func:`run_discovery_stage` — the stage itself: it persists the
  discovery record (durable identity + dispatch intent) BEFORE the
  read-only probes run, so a crash after dispatch still leaves an
  adoptable handle; it runs the bounded read-only discovery tools over
  the frozen repository snapshot; it writes the evidence artifact (the
  citations bundle) into the content-addressed store; and it produces
  the bounded evidence digest handed to the planner.
- :func:`attach_digest` / :func:`render_digest_section` — the planner's
  evidence digest: a delimited, canonically-serialized section whose
  entries carry ``file:line`` citations, capped so the planner's input
  budget is respected.
- :func:`validate_plan_citations` / :func:`enforce_plan_citations` —
  the NXT-06 slice: plan steps may cite ``evidence:<id>``; a citation
  that does not resolve to the digest's recorded evidence is REJECTED
  (fail-closed), while uncited claims stay allowed.

Durability follows the runs/ patterns: the record lives in the
``FlowRun.evidence`` blob (reassigned wholesale, like
``_merge_evidence``), and every stage transition writes its outbox row
in the same transaction (``discovery.started``, ``discovery.replayed``,
``plan.research_mode``). A restart adopts a completed discovery instead
of paying the probes again; a dispatched-but-never-completed record is
re-run under the SAME discovery id; a FAILED one is never silently
fallen back from — the stage raises.

NXT-07 (persisted clarification questions, answer-gated planning) lives
here too:

- the stage may EMIT bounded clarification questions (a
  ``question_source`` on the context): each carries durable identity
  (content-derived id — the same question never duplicates), the
  evidence ids it cites (validated against the recorded evidence,
  fail-closed), criticality and options; the record's status becomes
  ``waiting_question`` and the questions persist with it;
- while any question is unanswered, :func:`maybe_run_discovery` — the
  planning seam — REFUSES with a typed :class:`QuestionsOutstanding`
  carrying them (never a defaulted plan, never a silent pass);
- :func:`apply_answer_commands` (pure) + :func:`record_answers`
  (durable) consume the ``answer`` mailbox commands
  :meth:`~forge.adaptive.wiring.OperatorControlService.answer` records:
  one command resolves exactly ITS question id (answering Q2 never
  closes Q1), redelivery is idempotent, a foreign/unknown/empty answer
  has no effect, and the first recorded answer stands;
- once every question is answered (durably — the flip to ``complete``
  rides the same transaction as the last answer), planning resumes with
  the answers injected as a bounded, citation-validated
  :data:`ANSWERS_BEGIN`-delimited section beside the evidence digest.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from forge.adaptive.artifact_store import ContentAddressedStore
from forge.adaptive.discovery import DiscoveryRun, dispatch_target
from forge.adaptive.discovery_tools import SnapshotToolbox
from forge.durable import FlowRun, Outbox

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWERS_BEGIN",
    "ANSWERS_END",
    "DISCOVERY_ANSWERS_MAX_CHARS",
    "DISCOVERY_DIGEST_MAX_CHARS",
    "DIGEST_BEGIN",
    "DIGEST_END",
    "AnswerApplication",
    "AnswerDrain",
    "DiscoveryOutcome",
    "DiscoveryRunContext",
    "DiscoveryStageError",
    "EvidenceRecord",
    "FORGE_DISCOVERY_ENABLED_ENV",
    "InvalidPlanCitation",
    "PLANNER_INPUT_CAP_CHARS",
    "QuestionsOutstanding",
    "attach_answers",
    "attach_digest",
    "discovery_enabled",
    "enforce_plan_citations",
    "extract_citations",
    "extract_keywords",
    "evidence_ids_of",
    "apply_answer_command",
    "apply_answer_commands",
    "frozen_input_digest",
    "load_snapshot_files",
    "maybe_run_discovery",
    "open_question_ids_of",
    "open_questions_of",
    "record_answer",
    "record_answers",
    "render_answers_section",
    "render_digest_section",
    "run_discovery_stage",
    "snapshot_set_digest",
    "validate_plan_citations",
]

#: The env var that turns the stage on. Default OFF in this slice.
FORGE_DISCOVERY_ENABLED_ENV = "FORGE_DISCOVERY_ENABLED"

#: Truthy spellings accepted for the flag (mirrors lane_driver's env habits).
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Delimiters around the planner-facing evidence digest. A delimited
#: section survives the planner's prompt assembly unchanged and can be
#: stripped/inspected without parsing the whole prompt.
DIGEST_BEGIN = "<<<FORGE_DISCOVERY_EVIDENCE"
DIGEST_END = "FORGE_DISCOVERY_EVIDENCE>>>"

#: Delimiters around the operator-answers section (NXT-07): same
#: survival property, a different namespace — answers never masquerade
#: as evidence and vice versa.
ANSWERS_BEGIN = "<<<FORGE_DISCOVERY_ANSWERS"
ANSWERS_END = "FORGE_DISCOVERY_ANSWERS>>>"

#: The evidence digest budget (chars). The planner's total input cap is
#: :data:`PLANNER_INPUT_CAP_CHARS` (12000, mirroring
#: ``forge.factory.planner.PLANNER_MAX_INPUT_CHARS`` — kept as a local
#: constant so the adaptive substrate stays import-light; a unit test
#: pins the two equal). 4000 leaves the issue text the majority of the
#: budget while giving ~25 citable file:line entries.
DISCOVERY_DIGEST_MAX_CHARS = 4000
PLANNER_INPUT_CAP_CHARS = 12000

#: The answers-section budget (chars). The questions were bounded at
#: emission (:data:`_MAX_QUESTIONS`) and each answer is an operator's
#: one-liner; 2000 keeps issue + digest + answers inside the planner's
#: 12000 with the issue still the majority voice.
DISCOVERY_ANSWERS_MAX_CHARS = 2000

#: How many clarification questions one discovery may open (NXT-07). A
#: bounded wait: the operator answers a short list, not an interview.
_MAX_QUESTIONS = 3

#: The criticality vocabulary a question may carry. Unknown criticality
#: drops the question at emission (fail-closed — never a coerced guess).
_QUESTION_CRITICALITIES = frozenset({"critical", "advisory"})

#: How many issue keywords are probed, and how many results per keyword
#: per kind survive into the bundle. 6 keywords x (3 symbols + 3
#: references) bounds the worst-case bundle at 36 records.
_MAX_KEYWORDS = 6
_PER_KEYWORD_RESULTS = 3

#: Identifiers worth probing: a code-ish token of length >= 4.
_KEYWORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")

#: ``evidence:<id>`` references in plan step text (the digest's stated
#: citation syntax).
_CITATION_RE = re.compile(r"\bevidence:([A-Za-z0-9][A-Za-z0-9._-]*)")

#: Language keywords and filler words that must not become probes.
_STOPWORDS = frozenset(
    word.lower()
    for word in (
        # language keywords that clear the 4-char bar
        "async",
        "await",
        "class",
        "const",
        "false",
        "func",
        "import",
        "interface",
        "null",
        "none",
        "public",
        "return",
        "static",
        "struct",
        "true",
        "types",
        "void",
        "self",
        # issue-text filler
        "about",
        "after",
        "again",
        "because",
        "been",
        "before",
        "being",
        "both",
        "could",
        "does",
        "each",
        "from",
        "have",
        "here",
        "http",
        "https",
        "into",
        "issue",
        "many",
        "more",
        "most",
        "much",
        "only",
        "other",
        "over",
        "please",
        "should",
        "some",
        "such",
        "than",
        "that",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "under",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "whom",
        "whose",
        "will",
        "with",
        "would",
        "your",
        "description",
        "title",
    )
)

#: Where the durable record lives inside ``FlowRun.evidence``.
_RECORD_KEY = "discovery"
_RECORD_SCHEMA = "forge.discovery.stage-record/1"
_BUNDLE_SCHEMA = "forge.discovery.evidence-bundle/1"
_DIGEST_SCHEMA = "forge.discovery.digest/1"
_ANSWERS_SCHEMA = "forge.discovery.answers/1"

#: Snapshot loading caps: the stage reads a bounded, read-only view —
#: not the whole history of a huge monorepo — and stops loudly (logged)
#: at the caps rather than quietly overflowing the API process.
_SNAPSHOT_MAX_FILES = 200
_SNAPSHOT_MAX_TOTAL_BYTES = 4 * 1024 * 1024


class DiscoveryStageError(RuntimeError):
    """A discovery stage failure — LOUD on purpose.

    "Never silently fall back after discovery failure" (NXT-05): the
    stage raises instead of returning an unresearched planner input, so
    the caller parks the run rather than presenting a guess as a plan.
    """


class InvalidPlanCitation(ValueError):
    """A plan cited an evidence id that the digest never recorded."""


class QuestionsOutstanding(DiscoveryStageError):
    """Planning refuses: the run's discovery has unanswered questions (NXT-07).

    The typed refusal the planning seam raises instead of planning over
    defaults: it carries the durable question documents (id, text,
    criticality, citations, options) so the operator surface can render
    exactly what is being asked, and it stays raised until
    :func:`record_answers` durably resolves every question id. A
    subclass of :class:`DiscoveryStageError` on purpose — the stage's
    "loud, park the run" contract already covers it for callers that
    have not learned the specific type yet.
    """

    def __init__(
        self, discovery_id: str, run_id: str, questions: Iterable[Mapping[str, Any]]
    ) -> None:
        self.discovery_id = discovery_id
        self.run_id = run_id
        self.questions: tuple[dict[str, Any], ...] = tuple(dict(q) for q in questions)
        listed = ", ".join(str(q.get("question_id") or "?") for q in self.questions)
        super().__init__(
            f"discovery {discovery_id!r} for run {run_id!r} has "
            f"{len(self.questions)} unanswered clarification question(s) [{listed}]; "
            "planning refuses until /answer records an answer for each"
        )

    def summaries(self) -> list[str]:
        """One human line per outstanding question (id + criticality + text)."""
        return [
            f"[{q.get('criticality', 'critical')}] {q.get('question_id')}: {q.get('text', '')}"
            for q in self.questions
        ]


def discovery_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the durable discovery stage is turned on.

    Reads :data:`FORGE_DISCOVERY_ENABLED_ENV` (default OFF — the honest
    rollout: production behavior changes only when an operator opts in).
    """
    source = os.environ if env is None else env
    return str(source.get(FORGE_DISCOVERY_ENABLED_ENV, "")).strip().lower() in _TRUTHY


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def snapshot_set_digest(files: Mapping[str, str]) -> str:
    """sha256 over the snapshot's canonical ``{path: content-digest}``.

    Content addressing per path (not over raw bytes) keeps the digest
    stable under file ORDER, and equal snapshots hash equal — the
    identity a completed discovery binds to.
    """
    return _sha256(_canonical({path: _sha256(content) for path, content in files.items()}))


def frozen_input_digest(planner_input: str, snapshot_digest: str) -> str:
    """The digest of the FROZEN discovery input: issue text + snapshot.

    A replay adopts a completed discovery only when BOTH are unchanged:
    an edited issue (or a moved snapshot) is new input, not the run the
    record vouches for — the caller plans against fresh evidence or
    requests a revision explicitly (NXT-07 defers the revision flow).
    """
    return _sha256(f"{planner_input}\x00{snapshot_digest}")


def extract_keywords(text: str, *, limit: int = _MAX_KEYWORDS) -> list[str]:
    """Issue identifiers worth probing, in first-seen order, capped.

    Code-ish tokens (``LLMPlanner``, ``start_run``) survive; language
    keywords and filler words do not. The probe list is deterministic
    for a given input — the same issue always researches the same way.
    """
    seen: set[str] = set()
    keywords: list[str] = []
    for match in _KEYWORD_RE.findall(text or ""):
        if match.lower() in _STOPWORDS or match in seen:
            continue
        seen.add(match)
        keywords.append(match)
        if len(keywords) >= limit:
            break
    return keywords


def extract_citations(text: str) -> tuple[str, ...]:
    """Every distinct ``evidence:<id>`` reference in *text*, in order."""
    seen: set[str] = set()
    cited: list[str] = []
    for ref in _CITATION_RE.findall(text or ""):
        if ref not in seen:
            seen.add(ref)
            cited.append(ref)
    return tuple(cited)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    """One citable piece of repository evidence: file, line, provenance.

    ``content_digest`` is the sha256 of the cited line's bytes — what a
    verifier re-reads the snapshot to check (NXT-06's "the cited bytes
    were actually retrieved"). ``kind`` is ``symbol`` (a declaration
    found) or ``reference`` (a usage found).
    """

    evidence_id: str
    path: str
    line: int
    kind: str
    detail: str
    repository_id: str
    source_oid: str
    content_digest: str

    def as_document(self, *, text: str | None = None) -> dict[str, Any]:
        """The canonical JSON shape; *text* (the cited line) only in the
        full artifact bundle — the compact record and the planner digest
        stay bounded."""
        doc: dict[str, Any] = {
            "id": self.evidence_id,
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "detail": self.detail,
            "repository_id": self.repository_id,
            "source_oid": self.source_oid,
            "content_digest": self.content_digest,
        }
        if text is not None:
            doc["text"] = text
        return doc


@dataclass(frozen=True)
class DiscoveryOutcome:
    """What one :func:`run_discovery_stage` call established."""

    discovery_id: str
    replayed: bool
    repository_researched: bool
    evidence_ids: tuple[str, ...]
    artifact_digest: str
    digest_section: str
    record: dict[str, Any]
    open_question_ids: tuple[str, ...] = ()
    #: ids of clarification questions still unanswered in the durable
    #: record — non-empty means the stage landed in ``waiting_question``
    #: and planning must refuse (see :class:`QuestionsOutstanding`).


#: Anything that yields sessions — ``async_sessionmaker`` duck-types here.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: The snapshot source: a frozen mapping, or a zero-arg loader that
#: resolves one lazily (so a disabled stage never pays the read).
SnapshotSource = Mapping[str, str] | Callable[[], Awaitable[Mapping[str, str]]]

#: Where clarification questions come from (NXT-07): called AFTER the
#: read-only probes with the frozen planner input and the compact
#: evidence documents; returns proposed questions
#: (``{"text", "criticality", "citations", "options"}``). May be a plain
#: callable or an async one. The stage owns validation, identity,
#: bounding and persistence — the source only proposes.
QuestionSource = Callable[
    [str, list[dict[str, Any]]],
    "Iterable[Mapping[str, Any]] | Awaitable[Iterable[Mapping[str, Any]]]",
]


@dataclass(frozen=True)
class DiscoveryRunContext:
    """Everything the stage needs from the run it serves ("run_ctx").

    ``snapshot_files`` is the frozen repository snapshot the probes run
    against (path -> content). It may be a lazy loader so the splice can
    build the context cheaply before the gate check; the stage resolves
    it only when actually discovering. ``store`` is optional: with a
    content-addressed store the full bundle (with cited line text) is
    written as an artifact and its digest pinned into the record;
    without one, the compact records ride the run's evidence blob alone.
    """

    run_id: str
    project_id: int
    session_factory: SessionFactory
    snapshot_files: SnapshotSource
    repository_id: str = "default"
    source_oid: str = "HEAD"
    allowed_globs: list[str] | None = None
    store: ContentAddressedStore | None = None
    question_source: QuestionSource | None = None
    max_digest_chars: int = DISCOVERY_DIGEST_MAX_CHARS
    max_answer_chars: int = DISCOVERY_ANSWERS_MAX_CHARS
    max_keywords: int = _MAX_KEYWORDS
    max_questions: int = _MAX_QUESTIONS

    @classmethod
    def from_reader(
        cls,
        *,
        run_id: str,
        project_id: int,
        session_factory: SessionFactory,
        reader: Any,
        ref: str = "HEAD",
        repository_id: str = "default",
        allowed_globs: list[str] | None = None,
        store: ContentAddressedStore | None = None,
    ) -> DiscoveryRunContext:
        """A context whose snapshot loads lazily from a repository reader.

        ``reader`` duck-types the shared read surface
        (``get_tree`` / ``read_text`` — GitHubRepositoryReader,
        GitLabClient and the Azure reader all satisfy it). The loader is
        deferred: a disabled stage never touches the provider.
        """

        async def _load() -> Mapping[str, str]:
            return await load_snapshot_files(reader, project_id, ref, allowed_globs=allowed_globs)

        return cls(
            run_id=run_id,
            project_id=project_id,
            session_factory=session_factory,
            snapshot_files=_load,
            repository_id=repository_id,
            source_oid=ref,
            allowed_globs=allowed_globs,
            store=store,
        )


# ---------------------------------------------------------------------------
# Snapshot loading (read-only, provider-agnostic)
# ---------------------------------------------------------------------------


async def load_snapshot_files(
    reader: Any,
    project_id: int,
    ref: str,
    *,
    allowed_globs: list[str] | None = None,
    max_files: int = _SNAPSHOT_MAX_FILES,
    max_total_bytes: int = _SNAPSHOT_MAX_TOTAL_BYTES,
) -> dict[str, str]:
    """Load a bounded, read-only ``{path: content}`` snapshot at *ref*.

    Uses ONLY the duck-typed read surface (``get_tree`` / ``read_text``)
    — no clone, no write, no shell. Non-blob entries are skipped, paths
    outside ``allowed_globs`` do not exist for this snapshot (filtered
    BEFORE any content read), and the file/byte caps stop the load with
    a logged warning instead of an overflow. A file that fails to decode
    (binary content) is dropped with a warning — a coverage gap, never a
    crash of the stage.
    """
    tree = await reader.get_tree(project_id, "", ref, recursive=True)
    globs = ["**"] if not allowed_globs else list(allowed_globs)
    selected: list[str] = []
    for entry in tree:
        path = str(getattr(entry, "path", "") or "")
        if not path or str(getattr(entry, "type", "blob") or "blob") != "blob":
            continue
        if any(fnmatchcase(path, pattern) for pattern in globs):
            selected.append(path)
    selected = sorted(selected)[:max_files]
    if len(selected) == max_files:
        logger.warning(
            "discovery snapshot capped at %d files for ref %r — coverage is partial",
            max_files,
            ref,
        )
    files: dict[str, str] = {}
    total = 0
    for path in selected:
        try:
            text = await reader.read_text(path, ref)
        except Exception as exc:  # noqa: BLE001 — one bad blob must not sink the stage
            logger.warning("discovery snapshot: %s at %r unreadable: %s", path, ref, exc)
            continue
        if total + len(text) > max_total_bytes:
            logger.warning(
                "discovery snapshot byte cap (%d) reached after %d files — coverage is partial",
                max_total_bytes,
                len(files),
            )
            break
        files[path] = text
        total += len(text)
    return files


# ---------------------------------------------------------------------------
# The durable stage
# ---------------------------------------------------------------------------


async def maybe_run_discovery(run_ctx: DiscoveryRunContext, planner_input: str) -> str:
    """The splice seam: optionally discover, then hand back the input.

    The production /implement path calls this between run start and
    planning (exact patch in ``docs/adaptive/discovery-splice.md``).
    When the stage is disabled (the default) the input is returned
    UNTOUCHED and nothing is persisted — the classic workflow, byte for
    byte. When enabled, the durable stage runs and the returned input
    carries the bounded evidence digest section. Failures raise
    :class:`DiscoveryStageError` — never a silent fallback.

    NXT-07 answer-gating: when the durable record still has an
    unanswered clarification question, this seam REFUSES with
    :class:`QuestionsOutstanding` — the planner never sees the issue
    until every question is resolved. Once resolved, the returned input
    additionally carries the bounded operator-answers section.
    """
    if not discovery_enabled():
        return planner_input
    outcome = await run_discovery_stage(run_ctx, planner_input)
    outstanding = open_questions_of(outcome.record)
    if outstanding:
        raise QuestionsOutstanding(outcome.discovery_id, run_ctx.run_id, outstanding)
    augmented = attach_digest(planner_input, outcome.digest_section)
    answers_section = render_answers_section(outcome.record, max_chars=run_ctx.max_answer_chars)
    if not answers_section:
        return augmented
    return attach_answers(augmented, answers_section)


async def run_discovery_stage(run_ctx: DiscoveryRunContext, planner_input: str) -> DiscoveryOutcome:
    """Run (or adopt) the durable discovery stage for one run.

    Ordering is the durability story:

    1. freeze the input (issue text + snapshot digest);
    2. read the existing record — a COMPLETED match is REPLAYED (the
       probes are not re-paid; ``discovery.replayed`` is journaled), a
       FAILED one raises (no silent fallback), a DISPATCHED match is
       re-run under the same discovery id (crash recovery);
    3. persist the dispatch record + ``discovery.started`` outbox row
       BEFORE any probe runs;
    4. run the read-only bounded tools over the frozen snapshot;
    5. emit the bounded clarification questions (if a question source
       is configured): validated, deduped, durable — the record lands
       in ``waiting_question`` and planning refuses until answered;
    6. write the evidence artifact, persist the completed record and
       the ``plan.research_mode`` outbox row;
    7. render the bounded digest the planner will cite.
    """
    files = dict(await _resolve_snapshot(run_ctx))
    snap_digest = snapshot_set_digest(files)
    input_digest = frozen_input_digest(planner_input, snap_digest)

    existing = await _read_record(run_ctx.session_factory, run_ctx.run_id)
    if existing is not None:
        status = str(existing.get("status") or "")
        same_input = (
            str((existing.get("dispatch") or {}).get("frozen_input_digest") or "") == input_digest
        )
        if status in ("complete", "waiting_question") and same_input:
            return await _replay(run_ctx, existing)
        if status == "failed":
            reason = str(existing.get("block_reason") or "unknown")
            raise DiscoveryStageError(
                f"previous discovery {existing.get('discovery_id')!r} failed ({reason}); "
                "refusing to silently fall back to an unresearched plan"
            )

    # Crash recovery: a dispatched-but-never-completed record for the SAME
    # frozen input keeps its identity — the restart adopts the handle it
    # already announced instead of minting a second discovery.
    recovering = (
        existing is not None
        and str(existing.get("status")) == "dispatched"
        and (str((existing.get("dispatch") or {}).get("frozen_input_digest") or "") == input_digest)
    )
    discovery_id = (
        str(existing.get("discovery_id"))
        if recovering and existing is not None
        else f"disc-{uuid.uuid4().hex[:12]}"
    )

    record: dict[str, Any] = {
        "schema": _RECORD_SCHEMA,
        "discovery_id": discovery_id,
        "run_id": run_ctx.run_id,
        "status": "dispatched",
        "dispatch": {
            "target": dispatch_target(),
            "intent": "read_only_research",
            "executor": "snapshot_toolbox",
            "frozen_input_digest": input_digest,
            "snapshot_set_digest": snap_digest,
            "repository_id": run_ctx.repository_id,
            "source_oid": run_ctx.source_oid,
        },
        "supersedes": str(existing.get("discovery_id") or "") if existing else "",
        "evidence": [],
        "evidence_artifact_digest": "",
        "repository_researched": False,
        "replay_count": 0,
        "block_reason": "",
        "questions": [],
    }
    await _persist(
        run_ctx.session_factory,
        run_ctx.run_id,
        record,
        outbox_events=[
            (
                "discovery.started",
                {
                    "discovery_id": discovery_id,
                    "run_id": run_ctx.run_id,
                    "target": dispatch_target(),
                    "recovery": bool(recovering),
                },
            )
        ],
    )

    try:
        found, coverage, domain = _probe(run_ctx, discovery_id, snap_digest, files, planner_input)
    except Exception as exc:
        await _fail(run_ctx, record, str(exc))
        raise DiscoveryStageError(f"discovery {discovery_id!r} failed: {exc}") from exc

    # NXT-07: bounded question emission over the FROZEN evidence — the
    # domain object stacks every question and lands in waiting_question.
    questions = await _emit_questions(run_ctx, planner_input, found)
    for question in questions:
        domain = domain.raise_question(question["question_id"])

    records = [rec for rec, _text in found]
    artifact_digest = ""
    if run_ctx.store is not None and records:
        bundle = {
            "schema": _BUNDLE_SCHEMA,
            "discovery_id": discovery_id,
            "run_id": run_ctx.run_id,
            "snapshot_set_digest": snap_digest,
            "frozen_input_digest": input_digest,
            "records": [rec.as_document(text=text) for rec, text in found],
        }
        artifact_digest = run_ctx.store.put(
            _canonical(bundle).encode("utf-8"), content_type="application/json"
        )

    if domain.status == "waiting_question":
        # The evidence is durable, but planning must wait for answers:
        # the record says so, and the outbox row lands with it.
        record = {
            **record,
            "status": "waiting_question",
            "evidence": [rec.as_document() for rec in records],
            "evidence_artifact_digest": artifact_digest,
            "repository_researched": True,
            "coverage": coverage,
            "questions": questions,
        }
        await _persist(
            run_ctx.session_factory,
            run_ctx.run_id,
            record,
            outbox_events=[
                (
                    "discovery.questions_raised",
                    {
                        "discovery_id": discovery_id,
                        "run_id": run_ctx.run_id,
                        "question_ids": [q["question_id"] for q in questions],
                    },
                )
            ],
        )
        return DiscoveryOutcome(
            discovery_id=discovery_id,
            replayed=False,
            repository_researched=True,
            evidence_ids=tuple(rec.evidence_id for rec in records),
            artifact_digest=artifact_digest,
            digest_section=render_digest_section(record, max_chars=run_ctx.max_digest_chars),
            record=record,
            open_question_ids=tuple(q["question_id"] for q in questions),
        )

    domain = domain.complete()
    assert domain.status == "complete"  # the lifecycle object agrees the stage finished
    record = {
        **record,
        "status": "complete",
        "evidence": [rec.as_document() for rec in records],
        "evidence_artifact_digest": artifact_digest,
        "repository_researched": True,
        "coverage": coverage,
        "questions": questions,
    }
    await _persist(
        run_ctx.session_factory,
        run_ctx.run_id,
        record,
        outbox_events=[
            (
                "plan.research_mode",
                {
                    "mode": "evidence_backed",
                    "discovery_id": discovery_id,
                    "evidence_count": len(records),
                    "replayed": False,
                },
            )
        ],
    )
    return DiscoveryOutcome(
        discovery_id=discovery_id,
        replayed=False,
        repository_researched=True,
        evidence_ids=tuple(rec.evidence_id for rec in records),
        artifact_digest=artifact_digest,
        digest_section=render_digest_section(record, max_chars=run_ctx.max_digest_chars),
        record=record,
    )


async def _replay(run_ctx: DiscoveryRunContext, record: dict[str, Any]) -> DiscoveryOutcome:
    """Adopt a completed (or waiting) discovery: same identity, no probes re-paid."""
    bumped = {**record, "replay_count": int(record.get("replay_count") or 0) + 1}
    discovery_id = str(record.get("discovery_id"))
    open_ids = open_question_ids_of(record)
    events: list[tuple[str, dict[str, Any]]] = [
        ("discovery.replayed", {"discovery_id": discovery_id, "run_id": run_ctx.run_id})
    ]
    if open_ids:
        # Still waiting: planning cannot consume this yet, so the
        # research-mode announcement is withheld until the answers land.
        events.append(("discovery.waiting", {"discovery_id": discovery_id, "open": list(open_ids)}))
    else:
        events.append(
            (
                "plan.research_mode",
                {
                    "mode": "evidence_backed",
                    "discovery_id": discovery_id,
                    "evidence_count": len(record.get("evidence") or []),
                    "replayed": True,
                },
            )
        )
    await _persist(run_ctx.session_factory, run_ctx.run_id, bumped, outbox_events=events)
    return DiscoveryOutcome(
        discovery_id=discovery_id,
        replayed=True,
        repository_researched=True,
        evidence_ids=tuple(
            str(entry.get("id")) for entry in (record.get("evidence") or []) if entry.get("id")
        ),
        artifact_digest=str(record.get("evidence_artifact_digest") or ""),
        digest_section=render_digest_section(bumped, max_chars=run_ctx.max_digest_chars),
        record=bumped,
        open_question_ids=open_ids,
    )


async def _fail(run_ctx: DiscoveryRunContext, record: dict[str, Any], reason: str) -> None:
    """Persist the failure — loudly, and durably."""
    failed = {**record, "status": "failed", "block_reason": reason[:200]}
    try:
        await _persist(
            run_ctx.session_factory,
            run_ctx.run_id,
            failed,
            outbox_events=[
                (
                    "discovery.failed",
                    {"discovery_id": record.get("discovery_id"), "reason": reason[:200]},
                )
            ],
        )
    except Exception as exc:  # noqa: BLE001 — the original failure must still raise
        logger.error("could not persist discovery failure record: %s", exc)


def _probe(
    run_ctx: DiscoveryRunContext,
    discovery_id: str,
    snap_digest: str,
    files: Mapping[str, str],
    planner_input: str,
) -> tuple[list[tuple[EvidenceRecord, str]], dict[str, Any], DiscoveryRun]:
    """Deterministic read-only probes over the frozen snapshot.

    Identifiers extracted from the FROZEN issue text are looked up with
    the bounded toolbox (symbol declarations and usages); every hit
    becomes an :class:`EvidenceRecord` paired with the cited line's text
    (kept for the full artifact bundle only). The :class:`DiscoveryRun`
    lifecycle object is driven through start → record_evidence and
    returned STILL RUNNING — the stage (not the probe) decides whether
    the run completes or lands in ``waiting_question`` (NXT-07).
    """
    toolbox = SnapshotToolbox(files, allowed_globs=run_ctx.allowed_globs)
    keywords = extract_keywords(planner_input, limit=run_ctx.max_keywords)
    domain = DiscoveryRun(
        discovery_id=discovery_id,
        work_id=run_ctx.run_id,
        snapshot_set_digest=snap_digest,
    ).start()
    found: list[tuple[EvidenceRecord, str]] = []

    def _add(path: str, line: int, kind: str, detail: str, text: str) -> None:
        record = EvidenceRecord(
            evidence_id=f"ev-{len(found) + 1}",
            path=path,
            line=line,
            kind=kind,
            detail=detail,
            repository_id=run_ctx.repository_id,
            source_oid=run_ctx.source_oid,
            content_digest=_sha256(text),
        )
        found.append((record, text))

    for keyword in keywords:
        symbols = (toolbox.find_symbol(keyword).get("symbols") or [])[:_PER_KEYWORD_RESULTS]
        for symbol in symbols:
            path = str(symbol.get("path") or "")
            line_no = int(symbol.get("line_no") or 0)
            if not path or line_no < 1:
                continue
            window = toolbox.read_file(path, offset=line_no - 1, length=1)
            _add(
                path,
                line_no,
                "symbol",
                str(symbol.get("symbol") or keyword),
                str(window.get("content") or ""),
            )
        references = (toolbox.find_references(keyword).get("references") or [])[
            :_PER_KEYWORD_RESULTS
        ]
        for reference in references:
            path = str(reference.get("path") or "")
            line_no = int(reference.get("line_no") or 0)
            if not path or line_no < 1:
                continue
            _add(path, line_no, "reference", keyword, str(reference.get("text") or ""))

    for rec, _text in found:
        domain = domain.record_evidence(rec.evidence_id)
    coverage = {
        "keywords": keywords,
        "paths_visible": toolbox.path_count,
        "evidence_count": len(found),
    }
    return found, coverage, domain


async def _emit_questions(
    run_ctx: DiscoveryRunContext,
    planner_input: str,
    found: list[tuple[EvidenceRecord, str]],
) -> list[dict[str, Any]]:
    """Normalize the question source's proposals into durable questions.

    Every proposal is validated against the evidence the probes actually
    recorded: an unknown criticality or a citation of an evidence id
    that does not exist drops the question (fail-closed — a question may
    not lean on evidence discovery never gathered). Question identity is
    derived from the content (text + citations), so the same proposal
    can never open twice, and the list is bounded to
    ``run_ctx.max_questions``.
    """
    if run_ctx.question_source is None:
        return []
    known = {rec.evidence_id for rec, _text in found}
    proposed = run_ctx.question_source(planner_input, [rec.as_document() for rec, _text in found])
    if inspect.isawaitable(proposed):
        proposed = await proposed
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = 0
    for item in proposed:
        if len(questions) >= run_ctx.max_questions:
            dropped += 1
            continue
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        text = str(item.get("text") or "").strip()
        criticality = str(item.get("criticality") or "critical")
        citations = [str(c) for c in (item.get("citations") or []) if str(c)]
        if not text or criticality not in _QUESTION_CRITICALITIES:
            logger.warning(
                "discovery question dropped (empty text or unknown criticality %r)",
                criticality,
            )
            dropped += 1
            continue
        unknown = [c for c in citations if c not in known]
        if unknown:
            logger.warning("discovery question dropped: cites unknown evidence %s", unknown)
            dropped += 1
            continue
        question_id = (
            "q-" + _sha256(_canonical({"text": text, "citations": sorted(set(citations))}))[:10]
        )
        if question_id in seen:
            continue  # the same question content is ONE question
        seen.add(question_id)
        questions.append(
            {
                "question_id": question_id,
                "text": text,
                "criticality": criticality,
                "citations": sorted(set(citations)),
                "options": [str(o) for o in (item.get("options") or [])],
                "status": "open",
                "answer": None,
            }
        )
    if dropped:
        logger.info("discovery question emission dropped %d proposal(s)", dropped)
    return questions


async def _resolve_snapshot(run_ctx: DiscoveryRunContext) -> Mapping[str, str]:
    source = run_ctx.snapshot_files
    if callable(source):
        return await source()
    return source


# ---------------------------------------------------------------------------
# Persistence helpers (the runs/ patterns: evidence blob + outbox row)
# ---------------------------------------------------------------------------


async def _read_record(session_factory: SessionFactory, run_id: str) -> dict[str, Any] | None:
    """The run's persisted discovery record, or ``None`` when absent."""
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise DiscoveryStageError(f"flow run {run_id!r} not found")
        record = (run.evidence or {}).get(_RECORD_KEY)
        return dict(record) if isinstance(record, dict) else None


async def _persist(
    session_factory: SessionFactory,
    run_id: str,
    record: dict[str, Any],
    *,
    outbox_events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Write the record and its outbox rows in ONE transaction.

    Same shapes the runs services use: the ``FlowRun.evidence`` JSON is
    replaced with a fresh dict (in-place mutation of a JSON column is
    not change-tracked), and each outbox row lands in the same commit as
    the state it announces.
    """
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise DiscoveryStageError(f"flow run {run_id!r} not found")
        merged = dict(run.evidence or {})
        merged[_RECORD_KEY] = record
        run.evidence = merged
        for event_type, payload in outbox_events:
            session.add(Outbox(flow_run_id=run_id, event_type=event_type, payload=payload))
        await session.commit()


# ---------------------------------------------------------------------------
# The planner-facing digest
# ---------------------------------------------------------------------------


def _digest_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(entry.get("id") or ""),
        "path": str(entry.get("path") or ""),
        "line": int(entry.get("line") or 0),
        "kind": str(entry.get("kind") or ""),
        "detail": str(entry.get("detail") or ""),
    }


def _digest_document(
    record: Mapping[str, Any], entries: list[dict[str, Any]], *, dropped: int
) -> dict[str, Any]:
    return {
        "schema": _DIGEST_SCHEMA,
        "discovery_id": str(record.get("discovery_id") or ""),
        "repository_researched": bool(record.get("repository_researched")),
        "truncated": dropped > 0,
        "dropped": dropped,
        "evidence": entries,
        "rules": (
            "Plan steps may cite these entries as evidence:<id>. "
            "Citing an id not listed here is rejected."
        ),
    }


def render_digest_section(
    record: Mapping[str, Any], *, max_chars: int = DISCOVERY_DIGEST_MAX_CHARS
) -> str:
    """Render the delimited evidence digest, bounded to *max_chars*.

    Deterministic: canonical JSON (sorted keys), entries in recorded
    order. When the budget cannot hold every entry, entries are dropped
    from the END and ``truncated``/``dropped`` say so — a partial digest
    is honest, and citation validation runs against the durable record,
    so a truncation can never forge or hide a citable id.
    """
    entries = [_digest_entry(entry) for entry in (record.get("evidence") or [])]
    dropped = 0

    def _render(doc: dict[str, Any]) -> str:
        return f"{DIGEST_BEGIN}\n{_canonical(doc)}\n{DIGEST_END}"

    section = _render(_digest_document(record, entries, dropped=dropped))
    while len(section) > max_chars and entries:
        entries.pop()
        dropped += 1
        section = _render(_digest_document(record, entries, dropped=dropped))
    return section


def attach_digest(
    planner_input: str, digest_section: str, *, cap: int = PLANNER_INPUT_CAP_CHARS
) -> str:
    """Return the planner input with the digest section appended, bounded.

    The combined prompt stays within the planner's input cap: when the
    issue text alone would crowd the digest out, the ISSUE text is cut
    (head kept — exactly the cut the planner applies today) and a marker
    line says so. The digest itself is never cut by this function; it is
    already bounded by :func:`render_digest_section`.
    """
    digest_len = len(digest_section)
    if len(planner_input) + 2 + digest_len <= cap:
        return f"{planner_input}\n\n{digest_section}"
    marker = ""

    def _combined(kept: str) -> int:
        nonlocal marker
        marker = (
            f"(issue text truncated to {len(kept)} chars to fit the discovery digest "
            f"within the planner input cap)\n\n"
        )
        return len(kept) + 2 + len(marker) + digest_len

    #: 96 chars of headroom for the marker line (its length moves with
    #: the digit count of ``len(kept)``); the loop below closes the rest.
    kept = planner_input[: max(0, cap - digest_len - 2 - 96)]
    while kept and _combined(kept) > cap:
        kept = kept[:-1]
    return f"{kept}\n\n{marker}{digest_section}"


# ---------------------------------------------------------------------------
# Citation binding (the NXT-06 slice)
# ---------------------------------------------------------------------------


def evidence_ids_of(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The citable evidence ids of a persisted discovery record.

    Validation reads the ids from the DURABLE record — not from a
    re-parse of the (possibly planner-truncated) digest section — so the
    fail-closed set is exactly what discovery recorded.
    """
    return tuple(
        str(entry.get("id"))
        for entry in (record.get("evidence") or [])
        if isinstance(entry, Mapping) and entry.get("id")
    )


def validate_plan_citations(
    plan: Mapping[str, Any], known_evidence_ids: Collection[str]
) -> list[str]:
    """Every invalid citation in *plan*; empty means all citations resolve.

    Steps cite evidence as ``evidence:<id>`` in step text; dict-shaped
    steps (the :class:`~forge.adaptive.models.PlanStep` contract) may
    additionally list bare ids in ``evidence_refs``. A citation that does
    not resolve to a recorded evidence id is a violation naming the
    step. UNCITED steps are fine — the contract requires valid
    citations, not mandatory ones.
    """
    known = set(known_evidence_ids)
    steps = plan.get("steps") if isinstance(plan, Mapping) else None
    if not isinstance(steps, list):
        return []
    violations: list[str] = []
    for index, step in enumerate(steps, start=1):
        label = f"step {index}"
        text = ""
        if isinstance(step, Mapping):
            label = f"step {step.get('step_id', index)}"
            text = str(step.get("objective") or "")
            refs = step.get("evidence_refs")
            if isinstance(refs, list):
                for ref in refs:
                    ref_id = str(ref)
                    if ref_id.startswith("evidence:"):
                        ref_id = ref_id[len("evidence:") :]
                    if ref_id not in known:
                        violations.append(f"{label}: unknown evidence id {ref_id!r}")
        elif isinstance(step, str):
            text = step
        for cited in extract_citations(text):
            if cited not in known:
                violations.append(f"{label}: unknown evidence id {cited!r}")
    return violations


def enforce_plan_citations(plan: Mapping[str, Any], known_evidence_ids: Collection[str]) -> None:
    """Fail-closed citation validation: invalid citations REJECT the plan.

    Called at plan parse: the planner's JSON is accepted only when every
    ``evidence:<id>`` it cites exists in the discovery record's digest.
    """
    violations = validate_plan_citations(plan, known_evidence_ids)
    if violations:
        raise InvalidPlanCitation(
            f"plan cites {len(violations)} unknown evidence id(s): {'; '.join(violations)}"
        )


# ---------------------------------------------------------------------------
# Clarification questions and their answers (the NXT-07 slice)
# ---------------------------------------------------------------------------


def _questions_of(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(question)
        for question in (record.get("questions") or [])
        if isinstance(question, Mapping)
    ]


def open_questions_of(record: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The record's UNANSWERED questions — what planning is waiting on.

    Read from the DURABLE record, so the wait is exactly what persists:
    answering Q2 does not close Q1, and nothing but a recorded answer
    per question id clears this list.
    """
    return tuple(q for q in _questions_of(record) if str(q.get("status") or "") != "answered")


def open_question_ids_of(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The ids of the record's unanswered questions."""
    return tuple(
        str(q.get("question_id")) for q in open_questions_of(record) if q.get("question_id")
    )


def _command_view(command: Any) -> dict[str, Any]:
    """A plain-dict view of a mailbox command (Mapping or ControlCommand)."""
    if isinstance(command, Mapping):
        return dict(command)
    if hasattr(command, "model_dump"):
        return dict(command.model_dump())
    return {
        name: getattr(command, name)
        for name in ("kind", "payload", "actor_ref", "idempotency_key")
        if hasattr(command, name)
    }


@dataclass(frozen=True)
class AnswerApplication:
    """The verdict of applying ONE answer command to a discovery record.

    ``status`` is ``applied`` (the answer durably resolved ITS question),
    ``duplicate`` (redelivery of the command that already did — no
    effect, by design) or ``rejected`` (foreign scope, unknown question,
    empty text, or a second DIFFERENT answer to an already-answered
    question: the first answer stands). ``record`` is the updated record
    for ``applied`` and the unchanged input otherwise.
    """

    status: str
    reason: str
    question_id: str
    record: dict[str, Any]

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass(frozen=True)
class AnswerDrain:
    """The result of folding a batch of answer commands into a record."""

    record: dict[str, Any]
    applications: tuple[AnswerApplication, ...]

    @property
    def applied_count(self) -> int:
        return sum(1 for application in self.applications if application.applied)

    @property
    def open_question_ids(self) -> tuple[str, ...]:
        return open_question_ids_of(self.record)


def apply_answer_command(record: Mapping[str, Any], command: Any) -> AnswerApplication:
    """Pure: apply ONE mailbox answer command to a discovery record.

    The command is what :meth:`OperatorControlService.answer
    <forge.adaptive.wiring.OperatorControlService.answer>` recorded (a
    ``ControlCommand`` of kind ``answer`` whose payload carries
    ``question_id`` and ``text``; ``run_id`` optionally scopes it to one
    lane). This function is the /answer surface's seam into the durable
    discovery record — pure, so the wiring stays testable and thin:

    - the command resolves EXACTLY its own question id — answering Q2
      never closes Q1 (the review's counterexample);
    - a redelivery of the SAME command (same idempotency key) is a
      ``duplicate`` no-op;
    - a different answer to an already-answered question is
      ``rejected``: the first durable answer stands (the race resolution
      for two respondents);
    - unknown question ids, foreign run scopes and empty texts are
      ``rejected`` with no state change.
    """
    view = _command_view(command)
    base = dict(record)

    def _rejected(reason: str, question_id: str = "") -> AnswerApplication:
        return AnswerApplication("rejected", reason, question_id, base)

    if str(view.get("kind") or "") != "answer":
        return _rejected(f"command kind {view.get('kind')!r} is not an answer")
    payload = view.get("payload") or {}
    if not isinstance(payload, Mapping):
        return _rejected("answer command carries no payload")
    question_id = str(payload.get("question_id") or "")
    if not question_id:
        return _rejected("answer command carries no question_id")
    scoped_run = str(payload.get("run_id") or "")
    owning_run = str(base.get("run_id") or "")
    if scoped_run and scoped_run != owning_run:
        return _rejected(
            f"answer is scoped to run {scoped_run!r}; this discovery belongs to {owning_run!r}"
        )
    text = str(payload.get("text") or "").strip()
    if not text:
        return _rejected("answer text is empty", question_id)

    questions = _questions_of(base)
    match = next((q for q in questions if str(q.get("question_id") or "") == question_id), None)
    if match is None:
        return _rejected(f"unknown question id {question_id!r}", question_id)
    if str(match.get("status") or "") == "answered":
        recorded = match.get("answer") or {}
        if str(recorded.get("idempotency_key") or "") == str(view.get("idempotency_key") or ""):
            return AnswerApplication(
                "duplicate", "redelivered answer command; already recorded", question_id, base
            )
        return _rejected(
            f"question {question_id!r} is already answered; the first recorded answer stands",
            question_id,
        )

    answered = {
        **match,
        "status": "answered",
        "answer": {
            "text": text,
            "actor": str(view.get("actor_ref") or ""),
            "command_id": str(view.get("command_id") or ""),
            "idempotency_key": str(view.get("idempotency_key") or ""),
            "answered_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    updated = {
        **base,
        "questions": [
            answered if str(q.get("question_id") or "") == question_id else q for q in questions
        ],
    }
    return AnswerApplication("applied", "recorded", question_id, updated)


def apply_answer_commands(record: Mapping[str, Any], commands: Iterable[Any]) -> AnswerDrain:
    """Pure fold of answer commands (in sequence order) into the record.

    Each command applies to the record the PREVIOUS ones produced, so a
    mailbox drained in order resolves several questions in one pass and
    a redelivery after its original is the expected ``duplicate``.
    """
    current = dict(record)
    applications: list[AnswerApplication] = []
    for command in commands:
        application = apply_answer_command(current, command)
        applications.append(application)
        if application.applied:
            current = application.record
    return AnswerDrain(record=current, applications=tuple(applications))


async def record_answers(
    session_factory: SessionFactory, run_id: str, commands: Iterable[Any]
) -> AnswerDrain:
    """Durably apply answer commands to the run's discovery record.

    The /answer persistence leg: reads the durable record, folds the
    commands (pure), and — when at least one answer applied — rewrites
    the record and its outbox rows in ONE transaction. When the last
    open question resolves here, the record's status flips
    ``waiting_question`` → ``complete`` in the same commit (a crash
    between answer and flip cannot lose either), journaled as
    ``discovery.questions_resolved``; each applied answer also journals
    ``discovery.answer_recorded``.
    """
    command_list = list(commands)
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise DiscoveryStageError(f"flow run {run_id!r} not found")
        record = (run.evidence or {}).get(_RECORD_KEY)
        if not isinstance(record, dict):
            drain = apply_answer_commands({"run_id": run_id, "questions": []}, command_list)
            return AnswerDrain(
                record=drain.record,
                applications=tuple(
                    AnswerApplication(
                        "rejected",
                        "run has no persisted discovery record",
                        application.question_id,
                        dict(application.record),
                    )
                    for application in drain.applications
                ),
            )
        drain = apply_answer_commands(record, command_list)
        if drain.applied_count == 0:
            return drain
        new_record = drain.record

        def _actor_of(application: AnswerApplication) -> str:
            for question in new_record.get("questions") or []:
                if str(question.get("question_id") or "") == application.question_id:
                    return str((question.get("answer") or {}).get("actor") or "")
            return ""

        events = [
            (
                "discovery.answer_recorded",
                {
                    "run_id": run_id,
                    "discovery_id": str(new_record.get("discovery_id") or ""),
                    "question_id": application.question_id,
                    "actor": _actor_of(application),
                },
            )
            for application in drain.applications
            if application.applied
        ]
        if str(new_record.get("status")) == "waiting_question" and not open_question_ids_of(
            new_record
        ):
            new_record = {**new_record, "status": "complete"}
            events.append(
                (
                    "discovery.questions_resolved",
                    {
                        "run_id": run_id,
                        "discovery_id": str(new_record.get("discovery_id") or ""),
                    },
                )
            )
        merged = dict(run.evidence or {})
        merged[_RECORD_KEY] = new_record
        run.evidence = merged
        for event_type, payload in events:
            session.add(Outbox(flow_run_id=run_id, event_type=event_type, payload=payload))
        await session.commit()
        return AnswerDrain(record=new_record, applications=drain.applications)


async def record_answer(
    session_factory: SessionFactory, run_id: str, command: Any
) -> AnswerApplication:
    """Durably apply ONE answer command (see :func:`record_answers`)."""
    drain = await record_answers(session_factory, run_id, [command])
    return (
        drain.applications[0]
        if drain.applications
        else AnswerApplication("rejected", "no command supplied", "", dict())
    )


def _answer_entry(question: Mapping[str, Any]) -> dict[str, Any]:
    answer = question.get("answer") or {}
    return {
        "question_id": str(question.get("question_id") or ""),
        "question": str(question.get("text") or ""),
        "criticality": str(question.get("criticality") or "critical"),
        "citations": [str(c) for c in (question.get("citations") or [])],
        "answer": str(answer.get("text") or ""),
    }


def _answers_document(
    record: Mapping[str, Any], entries: list[dict[str, Any]], *, dropped: int
) -> dict[str, Any]:
    return {
        "schema": _ANSWERS_SCHEMA,
        "discovery_id": str(record.get("discovery_id") or ""),
        "truncated": dropped > 0,
        "dropped": dropped,
        "answers": entries,
        "rules": (
            "Operator answers to the discovery's clarification questions. "
            "Treat each answer as authoritative for the question asked; "
            "each entry's citations are evidence:<id> references recorded by discovery."
        ),
    }


def render_answers_section(
    record: Mapping[str, Any], *, max_chars: int = DISCOVERY_ANSWERS_MAX_CHARS
) -> str:
    """Render the delimited operator-answers section, bounded (NXT-07).

    Empty string when nothing is answered (nothing to inject). Like the
    evidence digest: deterministic canonical JSON, entries dropped from
    the END under budget with ``truncated``/``dropped`` saying so, and
    citation-validated — an entry whose citations no longer resolve
    against the durable evidence ids is dropped entirely rather than
    rendered leaning on evidence that is not there.
    """
    known = set(evidence_ids_of(record))
    entries: list[dict[str, Any]] = []
    dropped = 0
    for question in _questions_of(record):
        if str(question.get("status") or "") != "answered":
            continue
        citations = [str(c) for c in (question.get("citations") or [])]
        if any(citation not in known for citation in citations):
            dropped += 1
            continue
        entries.append(_answer_entry(question))

    def _render(doc: dict[str, Any]) -> str:
        return f"{ANSWERS_BEGIN}\n{_canonical(doc)}\n{ANSWERS_END}"

    if not entries and not dropped:
        return ""
    doc = _answers_document(record, entries, dropped=dropped)
    section = _render(doc)
    while len(section) > max_chars and entries:
        entries.pop()
        dropped += 1
        section = _render(_answers_document(record, entries, dropped=dropped))
    return section


def attach_answers(
    planner_input: str, answers_section: str, *, cap: int = PLANNER_INPUT_CAP_CHARS
) -> str:
    """Return the input with the answers section appended, within the cap.

    The same bargain as :func:`attach_digest`: the combined prompt stays
    within the planner's input cap by cutting the input's HEAD (the
    digest sits at the end of the base and survives), never the sections
    themselves — both are already bounded by their renderers.
    """
    section_len = len(answers_section)
    if len(planner_input) + 2 + section_len <= cap:
        return f"{planner_input}\n\n{answers_section}"
    marker = ""

    def _combined(kept: str) -> int:
        nonlocal marker
        marker = (
            f"(input truncated to {len(kept)} chars to fit the discovery answers "
            f"within the planner input cap)\n\n"
        )
        return len(kept) + 2 + len(marker) + section_len

    kept = planner_input[: max(0, cap - section_len - 2 - 96)]
    while kept and _combined(kept) > cap:
        kept = kept[:-1]
    return f"{kept}\n\n{marker}{answers_section}"
