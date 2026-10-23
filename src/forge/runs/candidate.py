"""Legacy attempt context adapter and candidate domain reexports."""

from __future__ import annotations

from dataclasses import dataclass

from forge.domain.candidate import (
    CreateFile as CreateFile,
    DeleteFile as DeleteFile,
    FullReplacement as FullReplacement,
    UnifiedPatch as UnifiedPatch,
    ChangeEntry as ChangeEntry,
    ChangeManifestEntry as ChangeManifestEntry,
    CandidateBundle as CandidateBundle,
    bundle_from_changeset as bundle_from_changeset,
    candidate_base_from_meta as candidate_base_from_meta,
    CandidateOperation as CandidateOperation,
)

from forge.domain.candidate_usage import (
    UsageCompleteness as UsageCompleteness,
    HarnessUsage as HarnessUsage,
    usage_document as usage_document,
    usage_receipt_id as usage_receipt_id,
)

from forge.domain.patch import (
    CandidateError as CandidateError,
    DiffHunk as DiffHunk,
    HunkLine as HunkLine,
    FORGE_MATERIALIZE_MAX_FILE_CHARS as FORGE_MATERIALIZE_MAX_FILE_CHARS,
    apply_unified_hunks as apply_unified_hunks,
    _enforce_cap as _enforce_cap,
    _blob_digests as _blob_digests,
    _sha256_digest as _sha256_digest,
    _digest_matches as _digest_matches,
)

from forge.domain.candidate_parser import (
    parse_unified_diff as parse_unified_diff,
)


@dataclass(frozen=True)
class AttemptContext:
    """The base snapshot set of ONE proposal attempt."""

    cycle: int
    #: read/materialize/validate/publish base for the WHOLE attempt.
    attempt_base: str
    #: frozen approved base — final cumulative review/acceptance only.
    source_base: str
    #: the verified candidate this attempt extends (None on cycle 1).
    previous_candidate: str | None

    @classmethod
    def of(cls, run: object) -> AttemptContext:
        """The context of *run*'s current attempt, from its durable state."""
        cycle = getattr(run, "commit_cycle", None) or 1
        source_base = str(getattr(run, "base_sha", None) or "")
        candidates = list(getattr(run, "candidate_shas", None) or [])
        previous = candidates[-1] if cycle > 1 and candidates else None
        return cls(
            cycle=cycle,
            attempt_base=previous or source_base,
            source_base=source_base,
            previous_candidate=previous,
        )

    def document(self) -> dict:
        """The evidence document: this attempt's number and bases."""
        return {
            "cycle": self.cycle,
            "attempt_base": self.attempt_base,
            "source_base": self.source_base,
            "previous_candidate": self.previous_candidate,
        }


def attempt_base_for(run: object) -> str:
    """The frozen attempt base for *run* (ADR-0016 §4): cycle 1 → approved source base; repair → the last verified candidate OID. The:class:`AttemptContext` shared by the harness lane (``FORGE_ATTEMPT_BASE``, checked against every candidate artifact) and validated adapter inputs."""
    return AttemptContext.of(run).attempt_base
