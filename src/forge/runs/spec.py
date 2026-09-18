"""The executable RunSpec (R04; ADR-0018 §1, contracts-v0.2 "RunSpec").

The RunSpec is not just a digest of what was approved — it IS the approved
input. Everything a post-approval leg needs to execute the run is frozen into
one canonical JSON document at plan acceptance: the task text, the plan
artifact, the model route, the tool/path policy, the verification contract,
the numeric budgets and the backend/driver. The pending decision binds the
document's digest; ``/go`` approves exactly those bytes. Consumption legs read
this document through :func:`load_verified_spec`, which re-computes the
canonical digest on EVERY read — a missing, tampered or legacy spec raises
:class:`SpecInvalid` and the run parks ``blocked(spec_invalid)``. There is no
silent fallback to live settings, ever.

Provider-neutral by construction (contracts-v0.2): the type knows nothing
about GitLab, GitHub or Azure DevOps — the subject is (provider, project id,
issue id) and the GitLab/GitHub/Azure services freeze and consume it with
their own readers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Schema version of the EXECUTABLE RunSpec document (v3): adds the frozen
#: task text, the plan artifact, the model route and the verification
#: contract to the v2 shape. v1/v2 documents carry digests only — they are
#: not executable and a verified read refuses them (fail closed).
EXECUTABLE_SPEC_SCHEMA_VERSION = 3

_DIGEST_LEN = 64


class SpecInvalid(Exception):
    """A stored RunSpec is missing, tampered with, legacy or unreadable.

    Raised by every verified read; the caller parks the run
    ``blocked(spec_invalid)`` — never falls back to live configuration.
    """


def canonical_json_digest(document: dict) -> str:
    """sha256 over the canonical (sorted-key) JSON of *document*."""
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def task_text_digest(title: str, description: str) -> str:
    """sha256 over the issue text — the task snapshot digest (ADR-0018 §2).

    The exact input the frozen task artifact carries, so a verified read can
    re-derive the digest from the stored text and detect internal tampering.
    """
    return hashlib.sha256(f"{title or ''}\n{description or ''}".encode("utf-8")).hexdigest()


def _digest_or_invalid(value: object, what: str) -> str:
    raw = str(value or "")
    if len(raw) != _DIGEST_LEN or any(c not in "0123456789abcdef" for c in raw):
        raise SpecInvalid(f"spec {what} is not a sha256 digest: {raw[:16]!r}")
    return raw


def _string_tuple(values: object, what: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise SpecInvalid(f"spec {what} is not a list")
    entries = tuple(str(entry) for entry in values)
    if any(not entry.strip() for entry in entries):
        raise SpecInvalid(f"spec {what} contains an empty entry")
    return entries


@dataclass(frozen=True)
class ExecutableRunSpec:
    """The typed, immutable executable input of one run (R04, ADR-0018 §1).

    Constructed only through :meth:`freeze` (plan-time capture) or
    :meth:`from_document` (verified read); ``__post_init__`` validates every
    field, so a malformed document cannot become an instance.
    """

    schema_version: int
    # Subject (contracts-v0.2: connection_id, provider, repository_id, issue_id).
    provider: str
    project_id: int
    issue_iid: int | None
    # Source snapshot.
    source_base_oid: str
    # Task artifact: the exact issue text the approver saw + its digest.
    task_title: str
    task_description: str
    task_digest: str
    # Plan artifact: the evidence representation of the approved plan.
    plan_summary: str
    plan_files_hint: tuple[str, ...]
    plan_digest: str
    # Model route (a tier reference resolved by the proxy — never a secret).
    model_route: str
    # Policy: the effective execution-policy digest + tool/path scope.
    policy_digest: str
    allowed_paths: tuple[str, ...]
    # Verification contract: the jobs a green pipeline must contain.
    required_jobs: tuple[str, ...]
    # Numeric budgets.
    commit_cycles: int
    harness_timeout: int
    # Backend / driver (ADR-0015/0023): the execution shape the gate approved.
    backend: str
    harness_model: str
    target_branch: str
    harness_driver: str
    harness_fallbacks: tuple[str, ...]
    budget_class: str
    selection_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or (
            self.schema_version < EXECUTABLE_SPEC_SCHEMA_VERSION
        ):
            raise SpecInvalid(
                f"spec schema v{self.schema_version} is not executable "
                f"(need v{EXECUTABLE_SPEC_SCHEMA_VERSION}+)"
            )
        if not self.provider.strip() or not self.backend.strip():
            raise SpecInvalid("spec subject and backend must be non-empty")
        if self.project_id < 1:
            raise SpecInvalid(f"spec project_id must be >= 1, got {self.project_id}")
        if not self.model_route.strip():
            raise SpecInvalid("spec model route must be non-empty")
        _digest_or_invalid(self.task_digest, "task_digest")
        _digest_or_invalid(self.plan_digest, "plan_digest")
        _digest_or_invalid(self.policy_digest, "policy_digest")
        if task_text_digest(self.task_title, self.task_description) != self.task_digest:
            raise SpecInvalid("spec task artifact does not match its task_digest")
        if self.commit_cycles < 1:
            raise SpecInvalid(f"spec commit_cycles must be >= 1, got {self.commit_cycles}")
        if self.harness_timeout < 1:
            raise SpecInvalid(f"spec harness_timeout must be >= 1, got {self.harness_timeout}")
        if not self.harness_driver.strip() or not self.target_branch.strip():
            raise SpecInvalid("spec harness driver and target branch must be non-empty")

    # -- construction ------------------------------------------------------

    @classmethod
    def freeze(
        cls,
        *,
        provider: str,
        project_id: int,
        issue_iid: int | None,
        source_base_oid: str,
        task_title: str,
        task_description: str,
        plan_summary: str,
        plan_files_hint: Sequence[str],
        plan_digest: str,
        model_route: str,
        policy_digest: str,
        required_jobs: Iterable[str],
        backend: str,
        harness_model: str,
        target_branch: str,
        harness_driver: str,
        harness_fallbacks: Sequence[str] = (),
        budget_class: str = "standard",
        selection_reason: str = "default",
        commit_cycles: int,
        harness_timeout: int,
        allowed_paths: Sequence[str] = (),
    ) -> ExecutableRunSpec:
        """Freeze the executable spec from plan-time values (F14, R04).

        Called at plan acceptance — before the plan is published — so the
        pending decision binds a digest over the exact bytes below. The task
        digest is derived here from the captured text; construction validates
        the result.
        """
        return cls(
            schema_version=EXECUTABLE_SPEC_SCHEMA_VERSION,
            provider=str(provider),
            project_id=int(project_id),
            issue_iid=None if issue_iid is None else int(issue_iid),
            source_base_oid=str(source_base_oid or ""),
            task_title=str(task_title or ""),
            task_description=str(task_description or ""),
            task_digest=task_text_digest(task_title, task_description),
            plan_summary=str(plan_summary or ""),
            plan_files_hint=_string_tuple(tuple(plan_files_hint), "plan_files_hint"),
            plan_digest=str(plan_digest or ""),
            model_route=str(model_route or ""),
            policy_digest=str(policy_digest or ""),
            allowed_paths=_string_tuple(tuple(allowed_paths), "allowed_paths"),
            required_jobs=_string_tuple(
                sorted({job.strip() for job in required_jobs if job.strip()}), "required_jobs"
            ),
            commit_cycles=int(commit_cycles),
            harness_timeout=int(harness_timeout),
            backend=str(backend or ""),
            harness_model=str(harness_model or ""),
            target_branch=str(target_branch or ""),
            harness_driver=str(harness_driver or ""),
            harness_fallbacks=_string_tuple(tuple(harness_fallbacks), "harness_fallbacks"),
            budget_class=str(budget_class or "standard"),
            selection_reason=str(selection_reason or "default"),
        )

    @classmethod
    def from_document(cls, document: dict) -> ExecutableRunSpec:
        """Parse + validate a stored v3 document; :class:`SpecInvalid` on defects."""
        subject = _section(document, "subject")
        task = _section(document, "task")
        plan = _section(document, "plan")
        route = _section(document, "model_route")
        verification = _section(document, "verification")
        budgets = _section(document, "budgets")
        backend_config = _section(document, "backend_config")
        try:
            return cls(
                schema_version=EXECUTABLE_SPEC_SCHEMA_VERSION,
                provider=str(subject.get("provider") or "gitlab"),
                project_id=int(subject.get("project_id") or 0),
                issue_iid=(
                    int(subject["issue_iid"]) if subject.get("issue_iid") is not None else None
                ),
                source_base_oid=str(document.get("source_base_oid") or ""),
                task_title=str(task.get("title") or ""),
                task_description=str(task.get("description") or ""),
                task_digest=_digest_or_invalid(task.get("digest"), "task digest"),
                plan_summary=str(plan.get("summary") or ""),
                plan_files_hint=_string_tuple(plan.get("files_hint"), "plan files_hint"),
                plan_digest=_digest_or_invalid(plan.get("digest"), "plan digest"),
                model_route=str(route.get("tier") or ""),
                policy_digest=_digest_or_invalid(document.get("policy_digest"), "policy digest"),
                allowed_paths=_string_tuple(document.get("allowed_paths"), "allowed_paths"),
                required_jobs=_string_tuple(verification.get("required_jobs"), "required_jobs"),
                commit_cycles=int(budgets.get("commit_cycles") or 0),
                harness_timeout=int(budgets.get("harness_timeout") or 0),
                backend=str(backend_config.get("backend") or ""),
                harness_model=str(backend_config.get("model") or ""),
                target_branch=str(backend_config.get("target_branch") or ""),
                harness_driver=str(backend_config.get("harness") or ""),
                harness_fallbacks=_string_tuple(
                    backend_config.get("harness_fallbacks"), "harness_fallbacks"
                ),
                budget_class=str(backend_config.get("budget_class") or "standard"),
                selection_reason=str(backend_config.get("selection_reason") or "default"),
            )
        except (ValueError, TypeError) as exc:
            # Numeric fields with non-numeric garbage etc. — still a corrupt
            # spec, never a crash past the verified read.
            raise SpecInvalid(f"spec document is unreadable: {exc}") from exc

    # -- views -------------------------------------------------------------

    def to_document(self) -> dict:
        """The canonical JSON document stored in ``run_specs`` (digest target).

        ``allowed_paths`` is present only for scoped runs, so an unscoped
        document stays byte-identical to the pre-v0.7 shape convention.
        """
        document: dict = {
            "subject": {
                "provider": self.provider,
                "project_id": self.project_id,
                "issue_iid": self.issue_iid,
            },
            "source_base_oid": self.source_base_oid,
            "plan_digest": self.plan_digest,
            "task_digest": self.task_digest,
            "policy_digest": self.policy_digest,
            "backend_config": {
                "backend": self.backend,
                "model": self.harness_model,
                "target_branch": self.target_branch,
                "harness": self.harness_driver,
                "harness_fallbacks": list(self.harness_fallbacks),
                "budget_class": self.budget_class,
                "selection_reason": self.selection_reason,
            },
            "budgets": {
                "commit_cycles": self.commit_cycles,
                "harness_timeout": self.harness_timeout,
            },
            "task": {
                "title": self.task_title,
                "description": self.task_description,
                "digest": self.task_digest,
            },
            "plan": {
                "summary": self.plan_summary,
                "files_hint": list(self.plan_files_hint),
                "digest": self.plan_digest,
            },
            "model_route": {"tier": self.model_route},
            "verification": {"required_jobs": list(self.required_jobs)},
        }
        if self.allowed_paths:
            document["allowed_paths"] = list(self.allowed_paths)
        return document

    @property
    def task_text(self) -> str:
        """The frozen task text — exactly the :func:`task_text_digest` input."""
        return f"{self.task_title or ''}\n{self.task_description or ''}"


def _section(document: dict, key: str) -> dict:
    section = document.get(key)
    if not isinstance(section, dict):
        raise SpecInvalid(f"spec is missing its {key!r} section")
    return section


def load_verified_spec(
    *,
    document: object,
    digest: str | None,
    run_spec_digest: str | None = None,
    schema_version: int | None = None,
) -> ExecutableRunSpec:
    """The digest-verified typed view of a stored ``run_specs`` row (R04).

    Every consumption read goes through here. Raises :class:`SpecInvalid` —
    never falls back — when the row is missing, its stored digest does not
    match the canonical JSON of its document (tampered/corrupt), its digest
    is not the one the gate froze into the run, or the document is a legacy
    (pre-v3) shape that carries no executable content.
    """
    if not isinstance(document, dict):
        raise SpecInvalid("no spec document is stored for this run")
    if not digest:
        raise SpecInvalid("spec row carries no digest")
    computed = canonical_json_digest(document)
    if computed != digest:
        raise SpecInvalid(
            f"spec digest mismatch: stored {str(digest)[:12]}, computed {computed[:12]} "
            "(the stored spec was tampered with or is corrupt)"
        )
    if run_spec_digest and digest != run_spec_digest:
        raise SpecInvalid(
            f"spec digest {digest[:12]} is not the gate-approved digest {run_spec_digest[:12]}"
        )
    if schema_version is not None and schema_version < EXECUTABLE_SPEC_SCHEMA_VERSION:
        raise SpecInvalid(
            f"legacy spec schema v{schema_version} is not executable "
            f"(need v{EXECUTABLE_SPEC_SCHEMA_VERSION})"
        )
    return ExecutableRunSpec.from_document(document)
