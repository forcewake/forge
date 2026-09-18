"""LLM implementer agent (ADR-0001/0004): issue + plan -> ChangeSet draft.

Pure with respect to run state: it reads GitLab (tree and file contents at
the run's pinned base snapshot) and calls the LLM, but never advances run
state and never writes to GitLab. It emits a strict-JSON ChangeSet draft that
:func:`forge.repository.changeset.materialize` turns into a typed ChangeSet
against the actual base content — exact-match replacements only, no fuzzy
apply, ever (ADR-0001).

Authority is never derived from the proposal: the branch and commit message
are pinned here from the trusted run identity, whatever the model returns.

R14: authoritative evidence is never conflated. The base reads behind
materialization go through :meth:`RepositoryReader.read_blob` typed results
(:mod:`forge.gitlab.blob_reads`) — only a provider-confirmed ``not_found``
proves a path absent (create allowed); ``forbidden`` / ``unavailable`` /
``incomplete`` raise :class:`AuthoritativeReadError` so the run blocks with
``authoritative_read_failed:`` evidence instead of a failed read silently
flipping an update into a create.

R32: the prompt budget reserves the critical sections. Task/constraints and
the repair diagnosis are assembled FIRST and are never truncated; file
evidence is packed into the remaining budget (plan-touched files first, the
biggest truncated first) and, when anything was cut, a machine-readable
evidence budget report ends the prompt — a tail truncation can only ever
consume evidence, never the diagnosis or the output contract.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Protocol

from forge.durable import factory_branch, short_run_id
from forge.factory.llm import LLMClient, parse_json, truncate_chars
from forge.repository.changeset import MaterializationError, materialize

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.durable import FlowRun
    from forge.gitlab.blob_reads import BlobReadResult
    from forge.gitlab.schemas import Issue, RepositoryFile, TreeEntry
    from forge.repository.changeset import ChangeSet

logger = logging.getLogger(__name__)

#: The code tier: implementation quality over latency.
IMPLEMENTER_TIER = "code"

#: Deterministic input budget (chars) for the proposal prompt (ADR-0013).
IMPLEMENTER_MAX_INPUT_CHARS = 24000

#: Repo-tree evidence caps: at most this many paths are shown.
IMPLEMENTER_MAX_TREE_PATHS = 200

#: At most this many files are inlined as content evidence...
IMPLEMENTER_MAX_FILES = 8

#: ...each capped at this many characters.
IMPLEMENTER_MAX_FILE_CHARS = 6000

#: Budget (chars) RESERVED for the repair diagnosis (R32). When a repair
#: context is present it is mandatory context — the reason this attempt
#: exists — so it is assembled BEFORE any file evidence and evidence packing
#: can only take the non-negative remainder of the budget: no amount of
#: evidence can ever push the repair text out of the prompt. The number is
#: the floor the reservation guarantees; real repair briefs are already
#: bounded upstream (the GitHub/Azure services pass <= 2000-char briefs, the
#: auto-repair loop passes the ADR-0013-bounded context) and are included in
#: full. Reported in the evidence budget report as ``repair_reserved_chars``.
IMPLEMENTER_REPAIR_RESERVED_CHARS = 2000

#: Below this much remaining evidence room a file is omitted WHOLE instead of
#: sliced to a sliver (R32): a 40-char fragment is not evidence, and the
#: budget report says the file was omitted so the gap is at least visible.
IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS = 200

#: Budget (chars) pre-reserved for the machine-readable evidence budget
#: report whenever file evidence is present (R32). Subtracting it up front
#: guarantees the report itself always fits inside the total budget, so the
#: completeness note can never be truncated away by the same overflow it
#: describes.
IMPLEMENTER_EVIDENCE_NOTE_ALLOWANCE_CHARS = 1024

#: Marker appended to a file block that was sliced to fit the budget (R32).
_EVIDENCE_TRUNCATION_MARKER = "\n[... file truncated to fit the prompt budget]"

#: Header line of the inlined file-evidence section (layout kept stable).
_EVIDENCE_HEADER = "Current file contents:"

#: Header line of the machine-readable completeness report (R32).
_EVIDENCE_NOTE_HEADER = "Evidence budget report (machine-readable):"

#: Hard safety cap (chars) for authoritative reads — the complete file texts
#: materialization runs against. Over this, proposing fails with a
#: :class:`MaterializationError` instead of silently truncating the base
#: (a truncated base would drop file tails from materialized updates).
FORGE_MATERIALIZE_MAX_FILE_CHARS = 400_000

#: Output can carry full file texts — allow more than the default budget.
IMPLEMENTER_MAX_TOKENS = 8192

_SYSTEM_PROMPT = (
    "You are the implementation agent of a code-writing bot for GitLab "
    "projects. You propose changes as a strict JSON ChangeSet.\n"
    "Respond with ONLY a JSON object:\n"
    '{"branch": "<given branch>", "commit_message": "<message>", '
    '"changes": [{"path": "...", "operation": "create|update|delete", '
    '"content": "<full text, create only>", '
    '"old_text": "<exact existing text, update only>", '
    '"new_text": "<replacement, update only>", "expected_matches": 1}]}\n'
    "Rules: for create, content is the FULL new file text. For update, "
    "old_text must appear EXACTLY expected_matches times in the current "
    "file content shown to you (default 1) — copy it byte-exact, no "
    "abbreviations. For delete, no content. Never touch CI config "
    "(.gitlab-ci.yml), forge config (.forge.yml), .github/ or lockfiles. "
    "Use the branch and commit message given in the task verbatim."
)


class AuthoritativeReadError(MaterializationError):
    """An authoritative base read failed without a provider-confirmed absence (R14).

    A 403, a timeout, or an undecodable payload is NOT evidence that a file
    is missing — treating it as one can flip an update into a create.
    Subclasses :class:`MaterializationError` so every caller that blocks a
    run on an inapplicable proposal blocks on this too, with the
    ``authoritative_read_failed:`` marker naming the real cause. ``detail``
    carries the read status and provider fragment for the run's evidence.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"authoritative_read_failed: {detail}")


class RepositoryReader(Protocol):
    """The authoritative read surface the implementer needs (ADR-0001/0019).

    :class:`~forge.gitlab.client.GitLabClient`,
    :class:`~forge.integrations.github.GitHubRepositoryReader` and
    :class:`~forge.integrations.azure.AzureRepositoryReader` all satisfy it
    structurally — the implementer is provider-agnostic (GitLab, GitHub or
    Azure DevOps).
    """

    async def get_file(
        self, project_id: int, file_path: str, ref: str = "HEAD"
    ) -> RepositoryFile: ...

    async def read_blob(
        self, project_id: int, file_path: str, ref: str = "HEAD"
    ) -> BlobReadResult: ...

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list[TreeEntry]: ...

    async def get_issue(self, project_id: int, issue_iid: int) -> Issue: ...


class LLMImplementer:
    """Proposes a ChangeSet for a run via the LiteLLM proxy + repo evidence."""

    def __init__(
        self,
        llm: LLMClient,
        gitlab: RepositoryReader,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._gitlab = gitlab
        self._settings = settings

    async def propose(
        self,
        run: FlowRun,
        issue_title: str,
        *,
        plan_summary: str = "",
        files_hint: list[str] | None = None,
        repair_context: str = "",
        attempt_base: str | None = None,
        task_text: str | None = None,
        model_route: str | None = None,
    ) -> ChangeSet:
        """Return a materialized :class:`ChangeSet` for *run*.

        *attempt_base* overrides the run's pinned base as the working
        snapshot to read (tree, evidence and authoritative contents) — a
        repair cycle passes the last verified candidate SHA so the repair
        extends that commit instead of rolling it back. None reads the
        pinned base as before.

        *task_text* is the FROZEN task snapshot from the executable RunSpec
        (R04, ADR-0018 §1): when given, it is the task prompt verbatim and
        the live issue is never re-read — the run executes exactly the text
        the approver saw, even if the issue has since drifted. None keeps
        the legacy live-read behavior (pre-spec callers).

        *model_route* is the model tier frozen in the RunSpec; None keeps
        the implementer's default tier.

        Raises :class:`MaterializationError` when the model's draft cannot be
        applied exactly against the base snapshot, and LLM errors propagate
        from the client.
        """
        base_sha = attempt_base or run.base_sha or "HEAD"
        paths = await self._tree_paths(run.project_id, base_sha)
        contents = await self._evidence_contents(run.project_id, base_sha, paths, files_hint or [])
        issue = (
            task_text
            if task_text is not None
            else await self._read_issue(run.project_id, run.issue_iid, issue_title)
        )

        # Trusted identity: whatever the model says, these pin the commit.
        branch = factory_branch(run.issue_iid, run.id)
        commit_message = f"forge: implement {run.issue_iid or 0} (run {short_run_id(run.id)})"

        user = self._build_user_prompt(
            issue=issue,
            plan_summary=plan_summary,
            paths=paths,
            contents=contents,
            branch=branch,
            commit_message=commit_message,
            repair_context=repair_context,
            files_hint=files_hint,
        )
        result = await self._llm.complete(
            tier=model_route or IMPLEMENTER_TIER,
            system=_SYSTEM_PROMPT,
            # R32: _build_user_prompt packs sections into the budget itself
            # (mandatory parts first, evidence last), so this deterministic
            # cut is only a pathological-input safety net — under normal
            # assembly it is a no-op and can only ever shave evidence.
            user=truncate_chars(user, IMPLEMENTER_MAX_INPUT_CHARS),
            role="implementer",
            flow_run_id=run.id,
            json_mode=True,
            max_tokens=IMPLEMENTER_MAX_TOKENS,
        )
        cs_raw = parse_json(result.text)

        # Untrusted proposal -> pinned authority + exact-match materialization.
        cs_raw["branch"] = branch
        cs_raw["commit_message"] = commit_message
        cs_raw["attempt_base_oid"] = base_sha

        touched = [
            path
            for change in (cs_raw.get("changes") or [])
            if isinstance(change, dict) and isinstance((path := change.get("path")), str)
        ]
        git_base = await self._authoritative_contents(run.project_id, base_sha, touched)
        return materialize(cs_raw, git_base)

    # ------------------------------------------------------------------
    # Evidence gathering (reads only, at the pinned base snapshot)
    # ------------------------------------------------------------------

    async def _tree_paths(self, project_id: int, base_sha: str) -> list[str]:
        try:
            entries = await self._gitlab.get_tree(project_id, ref=base_sha, recursive=True)
        except Exception:
            logger.warning(
                "Tree read failed for project %s — proposing without tree evidence",
                project_id,
                exc_info=True,
            )
            return []
        paths = [entry.path for entry in entries if entry.type == "blob"]
        return paths[:IMPLEMENTER_MAX_TREE_PATHS]

    async def _evidence_contents(
        self,
        project_id: int,
        base_sha: str,
        paths: list[str],
        files_hint: list[str],
    ) -> dict[str, str]:
        """Read the files the hints point at (exact, then by extension)."""
        selected = _select_evidence_files(paths, files_hint)
        return await self._fetch_contents(project_id, base_sha, selected)

    async def _authoritative_contents(
        self,
        project_id: int,
        base_sha: str,
        touched_paths: list[str],
    ) -> dict[str, str]:
        """Fetch COMPLETE content of the touched paths at the base snapshot.

        Unlike the evidence reads (bounded for the prompt budget), this is
        the authoritative base the ChangeSet is materialized against, so no
        truncation, ever — a truncated base would silently drop file tails
        from materialized updates. A file over the
        :data:`FORGE_MATERIALIZE_MAX_FILE_CHARS` safety cap raises
        :class:`MaterializationError` instead.

        R14: a path is treated as absent ONLY on a provider-confirmed
        ``not_found`` (materialize then treats it as "create is allowed /
        update or delete is impossible"). ``forbidden``, ``unavailable``
        and ``incomplete`` reads raise :class:`AuthoritativeReadError` —
        the caller blocks the run with the ``authoritative_read_failed``
        marker instead of letting a failed read flip an update into a
        create.
        """
        unique = list(dict.fromkeys(touched_paths))
        contents: dict[str, str] = {}
        for path in unique:
            result = await self._gitlab.read_blob(project_id, path, ref=base_sha)
            if result.confirmed_absent:
                continue
            if not result.usable:
                raise AuthoritativeReadError(
                    f"{path} at {base_sha[:8]} read {result.status}: {result.detail or 'no detail'}"
                )
            text = result.text()
            if len(text) > FORGE_MATERIALIZE_MAX_FILE_CHARS:
                raise MaterializationError(
                    f"file {path!r} is {len(text)} chars at {base_sha[:8]}, over the "
                    f"materialization cap of {FORGE_MATERIALIZE_MAX_FILE_CHARS} — "
                    "refusing to materialize against truncated content"
                )
            contents[path] = text
        return contents

    async def _fetch_contents(
        self,
        project_id: int,
        base_sha: str,
        paths: list[str],
        per_file_chars: int = IMPLEMENTER_MAX_FILE_CHARS,
        max_files: int = IMPLEMENTER_MAX_FILES,
    ) -> dict[str, str]:
        """Prompt-evidence reads (bounded, lenient) — NOT the authoritative base.

        A failed read here only shrinks what the model sees; it can never
        forge an existence fact, because materialization runs strictly
        against :meth:`_authoritative_contents` (R14).
        """
        contents: dict[str, str] = {}
        for path in paths[:max_files]:
            try:
                repo_file = await self._gitlab.get_file(project_id, path, ref=base_sha)
            except Exception:
                logger.info("File %r not readable at %s — skipped", path, base_sha[:8])
                continue
            contents[path] = truncate_chars(
                _decode(repo_file.content, repo_file.encoding), per_file_chars
            )
        return contents

    async def _read_issue(
        self,
        project_id: int,
        issue_iid: int | None,
        fallback_title: str,
    ) -> str:
        if issue_iid is None:
            return f"Issue title: {fallback_title}\n\nIssue description:\n(empty)"
        try:
            issue = await self._gitlab.get_issue(project_id, issue_iid)
        except Exception:
            logger.warning("Issue #%s read failed — using title only", issue_iid, exc_info=True)
            return f"Issue title: {fallback_title}\n\nIssue description:\n(empty)"
        return f"Issue title: {issue.title}\n\nIssue description:\n{issue.description or '(empty)'}"

    def _build_user_prompt(
        self,
        *,
        issue: str,
        plan_summary: str,
        paths: list[str],
        contents: dict[str, str],
        branch: str,
        commit_message: str,
        repair_context: str,
        files_hint: list[str] | None = None,
        budget_chars: int = IMPLEMENTER_MAX_INPUT_CHARS,
    ) -> str:
        """Assemble the user prompt under a reserved-section budget (R32).

        Section order and budget policy:

        1. **Task and constraints** (mandatory, never truncated): the issue,
           the plan summary, the repo tree, and the output contract (the
           pinned branch and commit message). Always first.
        2. **Repair diagnosis** (mandatory, reserved): on a repair cycle the
           CI-failure context is the reason this attempt exists, so it is
           assembled before any file evidence and consumes the budget before
           evidence is packed — evidence can never push it out (floor:
           :data:`IMPLEMENTER_REPAIR_RESERVED_CHARS`).
        3. **Code evidence** (gets the REMAINDER): file blocks packed in
           ranked order — plan-touched files first in hint order, then the
           remaining files smallest-first so the biggest ones sit at the
           budget boundary and are truncated/omitted first. A file that does
           not fully fit is sliced only above
           :data:`IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS`, otherwise omitted.
        4. **Evidence budget report**: when any evidence was truncated or
           omitted, a machine-readable JSON line listing included/truncated/
           omitted files with their sizes ends the prompt, so the model (and
           the logs) can see exactly how complete the evidence is.

        Because every mandatory part is assembled before the evidence and the
        evidence is packed to fit the remaining budget, a tail truncation
        (the safety net in :meth:`propose`) can only ever consume evidence
        and the report — never the diagnosis or the output contract.
        """
        head = "\n".join(
            [
                issue,
                "",
                f"Implementation plan (summary):\n{plan_summary or '(no plan summary available)'}",
                "",
                "Repository files (paths):\n" + "\n".join(paths),
                "",
                f"Use branch: {branch}\nUse commit message: {commit_message}",
            ]
        )

        # Mandatory prefix: task + contract (+ repair diagnosis). Evidence
        # never comes before this, so it can never push any of it out.
        mandatory = head
        repair_chars = 0
        if repair_context:
            mandatory += (
                "\n\n" + "A previous attempt was committed and its CI failed. Propose a "
                "REPAIR: fix the failing code on top of the previous change.\n"
                f"{repair_context}"
            )
            repair_chars = len(repair_context)

        if not contents:
            return mandatory

        # Evidence gets the remainder. The report room is reserved up front so
        # the completeness note can never be truncated by its own overflow,
        # and the joining blank line is part of the evidence cost.
        evidence_budget = (
            budget_chars - len(mandatory) - 2 - IMPLEMENTER_EVIDENCE_NOTE_ALLOWANCE_CHARS
        )
        section, included, truncated, omitted = _pack_evidence(
            contents, files_hint or [], max(0, evidence_budget)
        )
        prompt = mandatory + (("\n\n" + section) if section else "")

        if truncated or omitted:
            report = _evidence_budget_report(
                budget_chars, repair_chars, included, truncated, omitted
            )
            prompt += "\n\n" + report
        return prompt


def _select_evidence_files(
    paths: list[str],
    files_hint: list[str],
    max_files: int = IMPLEMENTER_MAX_FILES,
) -> list[str]:
    """Pick evidence files: exact hint matches first, then hint extensions."""
    if not files_hint:
        return []
    hint_paths = [h.strip() for h in files_hint if h.strip()]
    hint_extensions = {
        h.lower().lstrip(".").rsplit(".", 1)[-1]
        for h in hint_paths
        if h.strip().startswith(".") or "." in h.rsplit("/", 1)[-1]
    }

    selected: list[str] = []
    for path in paths:
        if path in hint_paths and path not in selected:
            selected.append(path)

    for path in paths:
        if len(selected) >= max_files:
            break
        if path in selected:
            continue
        extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if extension and extension in hint_extensions and path not in selected:
            selected.append(path)

    return selected[:max_files]


def _rank_evidence_paths(contents: dict[str, str], files_hint: list[str]) -> list[str]:
    """Rank evidence files for budget-aware packing (R32).

    Plan-touched files (the hint) come first in hint order; the remaining
    files follow smallest-first, so the biggest ones sit at the budget
    boundary and are the first to be truncated or dropped when evidence
    runs out of room.
    """
    ranked: list[str] = []
    seen: set[str] = set()
    for hint in files_hint:
        path = hint.strip()
        if path and path in contents and path not in seen:
            seen.add(path)
            ranked.append(path)
    ranked.extend(
        sorted(
            (path for path in contents if path not in seen),
            key=lambda path: (len(contents[path]), path),
        )
    )
    return ranked


def _pack_evidence(
    contents: dict[str, str],
    files_hint: list[str],
    budget: int,
) -> tuple[str, list[dict[str, str | int]], list[dict[str, str | int]], list[dict[str, str | int]]]:
    """Pack file evidence into *budget* chars (R32) and report what happened.

    Returns the ``Current file contents:`` section (possibly empty) plus
    per-file records for the budget report: ``included`` (whole file),
    ``truncated`` (head kept, marker appended) and ``omitted`` (no room).
    Files are packed in :func:`_rank_evidence_paths` order; once the budget
    is exhausted every remaining file is recorded as omitted.
    """
    blocks: list[str] = []
    included: list[dict[str, str | int]] = []
    truncated: list[dict[str, str | int]] = []
    omitted: list[dict[str, str | int]] = []
    used = len(_EVIDENCE_HEADER) + 1  # header line + its newline
    for path in _rank_evidence_paths(contents, files_hint):
        text = contents[path]
        prefix = f"--- FILE: {path} ---\n"
        gap = 2 if blocks else 0  # blank line between file blocks
        if used + gap + len(prefix) + len(text) <= budget:
            blocks.append(prefix + text)
            used += gap + len(prefix) + len(text)
            included.append({"path": path, "chars": len(text)})
            continue
        room = budget - used - gap - len(prefix) - len(_EVIDENCE_TRUNCATION_MARKER)
        if room >= IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS:
            kept = truncate_chars(text, room)
            blocks.append(prefix + kept + _EVIDENCE_TRUNCATION_MARKER)
            # The slice consumed the budget exactly — nothing after it fits.
            used += gap + len(prefix) + len(kept) + len(_EVIDENCE_TRUNCATION_MARKER)
            truncated.append({"path": path, "kept_chars": len(kept), "full_chars": len(text)})
        else:
            omitted.append({"path": path, "full_chars": len(text)})
    section = _EVIDENCE_HEADER + "\n" + "\n\n".join(blocks) if blocks else ""
    return section, included, truncated, omitted


def _evidence_budget_report(
    budget_chars: int,
    repair_chars: int,
    included: list[dict[str, str | int]],
    truncated: list[dict[str, str | int]],
    omitted: list[dict[str, str | int]],
) -> str:
    """The machine-readable completeness note that ends an overflowed prompt.

    Strict JSON on its own line: which evidence sections made it into the
    prompt and at what size, which were cut or left out — per R32 the model
    (and the run logs) can tell how complete the evidence it was shown is.
    """
    payload = {
        "budget_chars": budget_chars,
        "repair_reserved_chars": IMPLEMENTER_REPAIR_RESERVED_CHARS,
        "repair_context_included_chars": repair_chars,
        "evidence": {"included": included, "truncated": truncated, "omitted": omitted},
    }
    return _EVIDENCE_NOTE_HEADER + "\n" + json.dumps(payload, separators=(",", ":"))


def _decode(raw_content: str, encoding: str | None = None) -> str:
    """Decode a GitLab repository-file payload to text.

    EVIDENCE-path decoder only (prompt display, capped and truncated):
    undecodable bytes degrade to replacement characters there. The
    AUTHORITATIVE path uses :func:`forge.gitlab.blob_reads.decode_blob_content`,
    which rejects invalid UTF-8 as ``incomplete`` instead of mangling it
    (R14).
    """
    if encoding == "base64":
        return base64.b64decode(raw_content).decode("utf-8", errors="replace")
    # GitLab serves text files base64-encoded by default; the schema does not
    # always carry the encoding field, so probe before giving up.
    try:
        return base64.b64decode(raw_content, validate=True).decode("utf-8", errors="replace")
    except Exception:
        return raw_content


__all__ = [
    "AuthoritativeReadError",
    "FORGE_MATERIALIZE_MAX_FILE_CHARS",
    "IMPLEMENTER_EVIDENCE_NOTE_ALLOWANCE_CHARS",
    "IMPLEMENTER_MAX_INPUT_CHARS",
    "IMPLEMENTER_MAX_FILES",
    "IMPLEMENTER_MAX_FILE_CHARS",
    "IMPLEMENTER_MAX_TOKENS",
    "IMPLEMENTER_MAX_TREE_PATHS",
    "IMPLEMENTER_MIN_EVIDENCE_SLICE_CHARS",
    "IMPLEMENTER_REPAIR_RESERVED_CHARS",
    "IMPLEMENTER_TIER",
    "LLMImplementer",
    "MaterializationError",
]
