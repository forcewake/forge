"""The approved BriefEnvelope (A03): the lane executes the APPROVED bytes.

R05 bound the lane to the EXACT plan comment (by journaled id) but validated
only its author/header/run-id — the comment BODY could be edited after
``/go`` and would still pass, and the issue body was read LIVE for the task
text. The envelope closes that gap with content digests, not just identity:

- at freeze (plan acceptance, A02) the control plane freezes the approved
  brief bytes — task title, task description, plan text — into the run's
  evidence as a versioned envelope, each byte field carrying its own sha256
  and the whole document bound by ``envelope_digest`` = sha256 over the
  canonical (sorted-key) JSON of ``run_id`` + the task bytes + the plan
  bytes + the frozen ``spec_digest`` (the A02 RunSpec digest — reusing the
  spec's binding instead of re-inventing one);
- the plan comment renders the SAME bytes between machine-delimited
  section markers (:func:`render_approved_sections`) — the comment stays
  the human-readable representation; the markers make the approved spans
  byte-extractable;
- at dispatch the control plane passes ``envelope_digest`` + ``spec_digest``
  (+ the existing ``plan_note_id``); the lane fetches that one comment,
  extracts the approved sections (:func:`extract_approved_sections`),
  re-computes the envelope digest over the extracted bytes
  (:func:`verify_brief_envelope`) and FAILS CLOSED on any mismatch —
  "approved brief bytes changed after approval (re-approval required)".
  The task text never comes from the live issue again: the brief is
  rendered from the digest-verified frozen bytes only.

Marker-injection note: issue text that embeds the marker lines can only
split the extraction differently — acceptance requires the re-computed
envelope digest to equal the dispatched one, and sha256 pins the exact
bytes — so injection can fail the lane closed, never smuggle bytes in.

Pure stdlib, importable by both the control plane and the stdlib-only
Actions lane (:mod:`forge.harness_entry`) — no forge database, no forge
state, no heavy storage service: the envelope is one JSON document riding
the run's evidence.
"""

from __future__ import annotations

import hashlib
import json
import re

#: Schema version of the envelope document. Bump on any shape change — a
#: lane that understands only v1 refuses nothing outright (it re-computes
#: the digest over the v1 members), but a future format must not silently
#: reinterpret v1 documents.
BRIEF_ENVELOPE_SCHEMA_VERSION = 1

#: The plan-comment section markers (HTML comments: invisible in the
#: rendered comment on GitHub, byte-stable in the raw body). Layout:
#: ``plan`` span first (the plan body sits under the header, as before),
#: then the task title + description spans, then the end sentinel.
PLAN_SECTION_START = "<!-- forge:brief:plan -->"
TASK_SECTION_START = "<!-- forge:brief:task -->"
TASK_DESCRIPTION_MARKER = "<!-- forge:brief:task-description -->"
ENVELOPE_SECTION_END = "<!-- forge:brief:end -->"

#: Byte-exact section extraction: each span is delimited by the marker
#: lines themselves (``marker\\n`` opens, ``\\nmarker`` closes), so the
#: captured groups ARE the approved bytes — including empty and
#: multi-line content, losslessly. Non-greedy: marker strings embedded in
#: the approved content split early, which the envelope digest then
#: rejects (fail closed) — never accepts.
_SECTION_RE = re.compile(
    re.escape(PLAN_SECTION_START)
    + r"\n(.*?)\n"
    + re.escape(TASK_SECTION_START)
    + r"\n(.*?)\n"
    + re.escape(TASK_DESCRIPTION_MARKER)
    + r"\n(.*?)\n"
    + re.escape(ENVELOPE_SECTION_END),
    re.DOTALL,
)


class BriefEnvelopeError(ValueError):
    """The approved brief sections are missing or fail the envelope digest.

    The lane converts this to a fail-closed brief refusal; the control
    plane never expects it (it renders the sections from the frozen bytes
    itself).
    """


def brief_bytes_digest(text: str) -> str:
    """sha256 over the exact UTF-8 bytes of one approved brief field."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_digest(document: dict) -> str:
    """sha256 over the canonical (sorted-key) JSON of *document*.

    Byte-identical to :func:`forge.runs.spec.canonical_json_digest` (the
    same ``json.dumps`` arguments) — re-implemented here so the stdlib-only
    lane never imports the control-plane package tree.
    """
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def build_brief_envelope(
    *,
    run_id: str,
    task_title: str,
    task_description: str,
    plan_text: str,
    spec_digest: str,
) -> dict:
    """Freeze the approved brief bytes into the envelope document (A03).

    Called at plan acceptance with the exact bytes the plan comment will
    render and the digest of the spec frozen in the same transaction. Every
    byte field carries its own sha256; ``envelope_digest`` binds the whole
    document (``run_id`` + task bytes + plan bytes + ``spec_digest``) and
    is the value dispatched to the lane for fail-closed re-verification.
    """
    document = {
        "schema_version": BRIEF_ENVELOPE_SCHEMA_VERSION,
        "run_id": run_id,
        "task_title": task_title,
        "task_title_digest": brief_bytes_digest(task_title),
        "task_description": task_description,
        "task_description_digest": brief_bytes_digest(task_description),
        "plan_text": plan_text,
        "plan_text_digest": brief_bytes_digest(plan_text),
        "spec_digest": spec_digest,
    }
    return {**document, "envelope_digest": _canonical_digest(document)}


def verify_brief_envelope(
    envelope_digest: str,
    *,
    run_id: str,
    task_title: str,
    task_description: str,
    plan_text: str,
    spec_digest: str,
) -> None:
    """Re-compute the envelope digest over the extracted bytes — fail closed.

    *envelope_digest* is the DISPATCHED value (out of band, untouchable by
    comment edits); the remaining arguments are the bytes extracted from the
    plan comment. Any difference — an edited comment body, a tampered
    digest, a wrong run/spec binding — raises :class:`BriefEnvelopeError`
    with the re-approval message; there is no warning-only mode.
    """
    expected = build_brief_envelope(
        run_id=run_id,
        task_title=task_title,
        task_description=task_description,
        plan_text=plan_text,
        spec_digest=spec_digest,
    )["envelope_digest"]
    if expected != envelope_digest:
        raise BriefEnvelopeError(
            "approved brief bytes changed after approval (re-approval required)"
        )


def render_approved_sections(*, task_title: str, task_description: str, plan_text: str) -> str:
    """The plan-comment block carrying the approved bytes between markers.

    The exact inverse of :func:`extract_approved_sections` — one renderer,
    one parser, round-trip tested, so the lane's extraction is byte-faithful
    by construction.
    """
    return (
        f"{PLAN_SECTION_START}\n"
        f"{plan_text}\n"
        f"{TASK_SECTION_START}\n"
        f"{task_title}\n"
        f"{TASK_DESCRIPTION_MARKER}\n"
        f"{task_description}\n"
        f"{ENVELOPE_SECTION_END}"
    )


def extract_approved_sections(body: str) -> tuple[str, str, str]:
    """Extract the approved bytes — (task_title, task_description, plan_text).

    Raises :class:`BriefEnvelopeError` when the comment carries no envelope
    sections at all (a pre-A03 comment can never satisfy an enforced
    envelope — fail closed, never guess).
    """
    match = _SECTION_RE.search(body or "")
    if match is None:
        raise BriefEnvelopeError(
            "plan comment carries no approved brief sections "
            f"({PLAN_SECTION_START!r} .. {ENVELOPE_SECTION_END!r}) — refusing the brief"
        )
    plan_text, task_title, task_description = match.groups()
    return task_title, task_description, plan_text
