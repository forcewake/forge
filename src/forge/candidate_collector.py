"""forge candidate_collector — collect the candidate from the ACTIVE workspace (Q35-01).

The shipped GitHub harness template's "Emit candidate artifact" step used
to run ``git add -A`` + ``git diff --cached`` in the ORIGINAL checkout.
After a cross-runner resume that is the WRONG tree: the lane
(:mod:`forge.lane_driver`) restores WIP into a stable SIBLING generation
(``<parent>/.forge-workspace-gen-<checkpoint_id[:12]>``), ``os.chdir()``s
the LANE PROCESS into it (the agent's edits land there), and records the
active generation in the checkout's ``.forge/workspace-generation``
pointer — a separate Actions ``run`` starts in a fresh shell at the
checkout, so the inline commands diffed the untouched base and the
uploaded candidate could be 0 bytes while the real work sat in the
generation (the reviewer's P01 characterization: a 0-byte diff beside a
431-byte change).

This module is the packaged collector the template now invokes
(``python -m forge.harness_entry --collect-candidate``):

- it resolves the tree to collect through the CHECKOUT'S pointer — never
  through an inherited cwd — and VALIDATES the pointer's ownership
  before anything runs: the generation must be a direct sibling named
  ``.forge-workspace-gen-<hex>`` (the only shape :mod:`forge.adaptive.
  checkpointing` mints), the pointer's ``work_id`` must equal the
  expected work id, and any absolute ``generation_path`` the document
  carries must agree with the sibling resolution. A forged or foreign
  pointer is a :class:`CollectionRefused` with the reason and produces
  ZERO artifacts — an agent-written absolute path is never publication
  authority;
- it runs REAL Git (``subprocess``) with explicit ``git -C`` on the
  VALIDATED tree: staging and the frozen-base diff never depend on the
  caller's cwd. A Git failure is a :class:`CollectionError` carrying
  git's stderr — the old ``|| true`` (a failed diff silently becoming
  an empty "successful" candidate) is structurally impossible here. A
  ZERO-CHANGE candidate (empty diff, exit 0) is a VALID result flagged
  ``zero_change`` — a no-op turn is never confused with a failed Git
  command, and neither is classified as the other;
- it removes lane-infrastructure paths inside the COLLECTED tree only
  (``.codegraph``, ``.venv``, ``__pycache__``, ``.pytest_cache`` and
  every ``*.pyc`` — the same cleanup list the template step carried),
  and writes ``candidate.diff`` into an output root OUTSIDE the
  collected generation (default ``<checkout_root>/forge-output``), so
  the candidate bytes can never stage themselves;
- a MISSING pointer with ``allow_missing_pointer=True`` is the fresh-run
  path (no restore happened; the agent worked in the checkout itself —
  today's no-resume flow, byte-compatible: same commands, same output
  path, same emit-meta contract). With ``allow_missing_pointer=False``
  a missing pointer is the typed :class:`GenerationPointerMissing` —
  never a silent fallback to the checkout.

Stdlib + subprocess Git only — no forge imports: the module stays
importable wherever the pinned lane package runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CANDIDATE_DIFF_NAME",
    "GENERATION_POINTER",
    "GENERATION_POINTER_SCHEMA",
    "CollectionError",
    "CollectionRefused",
    "CollectionResult",
    "GenerationPointerMissing",
    "INFRASTRUCTURE_DIRS",
    "collect_candidate",
]

#: The lane's active-generation pointer, read from the STABLE checkout
#: (written by :func:`forge.adaptive.checkpointing._write_generation_pointer`
#: — the exact contract this collector resolves, never a guessed shape).
GENERATION_POINTER = ".forge/workspace-generation"

#: The pointer document's schema tag (checkpointing's
#: ``forge.workspace-generation/1`` — anything else is not a pointer this
#: collector minted trust for).
GENERATION_POINTER_SCHEMA = "forge.workspace-generation/1"

#: The sibling-generation name prefix checkpointing's ``promote="generation"``
#: restore mints: ``.forge-workspace-gen-<checkpoint_id[:12]>`` (the
#: checkpoint id is a sha256 hex digest, so the fragment is hex).
_GENERATION_PREFIX = ".forge-workspace-gen-"
_GENERATION_NAME_RE = re.compile(r"^\.forge-workspace-gen-[0-9a-f]+$")

#: Lane-infrastructure directories physically removed from the COLLECTED
#: tree before staging — the template emit step's cleanup list, mirrored.
#: Top-level names only, exactly like the shipped ``rm -rf`` lines.
INFRASTRUCTURE_DIRS = (".codegraph", ".venv", "__pycache__", ".pytest_cache")

#: The staged diff's basename (the byte-for-byte contract
#: ``harness_entry --emit-meta`` consumes at
#: ``<output_root>/candidate.diff``).
CANDIDATE_DIFF_NAME = "candidate.diff"

#: Default output root: the checkout's non-hidden staging directory —
#: OUTSIDE any sibling generation, and the same path the upload step
#: and emit-meta already use.
DEFAULT_OUTPUT_DIRNAME = "forge-output"

#: One bound on every Git invocation: a hung Git must surface as a typed
#: collection error, never an indefinitely green step.
_GIT_TIMEOUT_S = 1800.0


class CollectionError(Exception):
    """A candidate collection failed — Git error, unsupported layout, or
    caller misuse. The invoking step must FAIL (non-zero), never upload a
    substitute diff."""


class CollectionRefused(CollectionError):
    """The generation pointer failed OWNERSHIP validation — forged shape,
    foreign work id, or an absolute path outside the owned sibling
    pattern. Zero artifacts are produced; the reason names the refusal."""


class GenerationPointerMissing(CollectionError):
    """No ``.forge/workspace-generation`` pointer exists and the caller
    required one (``allow_missing_pointer=False``) — a typed error, never
    a silent fallback to collecting the checkout."""


@dataclass(frozen=True)
class CollectionResult:
    """What one collection produced, and from WHICH tree.

    ``generation_path`` is the ABSOLUTE path of the tree actually
    collected (the validated generation, or the checkout itself on the
    fresh-run path); ``resolved_work_id`` / ``checkpoint_id`` are the
    pointer's identity for a generation collection and the expected work
    id / ``""`` for a checkout collection; ``zero_change`` flags the
    VALID empty diff (exit 0) so a no-op candidate is distinguishable
    from a failed Git command by type, never by string matching.
    """

    #: Where ``candidate.diff`` was written (outside the collected tree
    #: for a generation collection).
    diff_path: Path
    #: sha256 hex digest over the exact diff bytes (the same binding
    #: emit-meta's ``manifest_digest`` carries).
    diff_digest: str
    #: Absolute path of the tree the bytes were collected from.
    generation_path: str
    #: The work id the collected tree is bound to.
    resolved_work_id: str
    #: The checkpoint id that minted the generation ("" on fresh runs).
    checkpoint_id: str
    #: The frozen base the diff was taken against.
    base_oid: str
    #: True only for a SUCCESSFUL empty diff (a no-op candidate).
    zero_change: bool
    #: ``"generation"`` (pointer-resolved) | ``"checkout"`` (fresh run).
    source: str

    def as_dict(self) -> dict[str, object]:
        """A JSON-ready view (the CLI prints this for the step log)."""
        return {
            "diff_path": str(self.diff_path),
            "diff_digest": self.diff_digest,
            "generation_path": self.generation_path,
            "resolved_work_id": self.resolved_work_id,
            "checkpoint_id": self.checkpoint_id,
            "base_oid": self.base_oid,
            "zero_change": self.zero_change,
            "source": self.source,
        }


def _run_git(tree: Path, args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    """Run ONE ``git -C <tree> ...`` invocation, byte-captured. A timeout
    surfaces as a typed :class:`CollectionError`, never a hang."""
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(tree), *args],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectionError(
            f"git {' '.join(args)} in {tree} exceeded {_GIT_TIMEOUT_S:.0f}s — "
            "the collection step refuses to wait on a hung Git"
        ) from exc


def _require_git_worktree(tree: Path) -> None:
    """Fail typed on a tree Git cannot collect from — BEFORE any command.

    A generation is a full ``copytree`` of the checkout (``.git``
    directory included), so a regular repository collects fine. A
    ``.git`` FILE is a linked-worktree pointer whose target is NOT
    carried into the generation (the issue's unsupported-layout case),
    and a missing ``.git`` is not a repository at all — both are typed
    :class:`CollectionError` reasons, never a Git stderr to decode.
    """
    git_dir = tree / ".git"
    if not git_dir.exists():
        raise CollectionError(
            f"{tree} is not a Git repository (no .git) — the candidate cannot be collected from it"
        )
    if not git_dir.is_dir():
        raise CollectionError(
            f"{tree} carries a linked-worktree .git pointer — that topology is not "
            "carried into a generation; refusing to collect from it"
        )


def _resolve_pointer(checkout_root: Path, expected_work_id: str) -> tuple[Path, str, str]:
    """Resolve and OWNERSHIP-VALIDATE the checkout's generation pointer.

    Returns ``(generation_dir, work_id, checkpoint_id)`` — the absolute
    sibling directory the pointer's NAME resolves to (never a path read
    out of the document), plus the pointer's identity. Every failure is
    a :class:`CollectionRefused` naming the exact reason; nothing is
    written by this function.
    """
    pointer_path = checkout_root / GENERATION_POINTER
    try:
        document = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CollectionRefused(
            f"generation pointer {pointer_path} is unreadable or not valid JSON "
            f"({exc}) — refusing to collect"
        ) from exc
    if not isinstance(document, dict):
        raise CollectionRefused(
            f"generation pointer {pointer_path} is not a JSON object — refusing to collect"
        )
    schema = str(document.get("schema") or "")
    if schema != GENERATION_POINTER_SCHEMA:
        raise CollectionRefused(
            f"generation pointer {pointer_path} carries schema {schema!r} — expected "
            f"{GENERATION_POINTER_SCHEMA!r}; refusing to collect"
        )
    expected = (expected_work_id or "").strip()
    work_id = str(document.get("work_id") or "").strip()
    if not expected:
        raise CollectionRefused(
            "no expected work id was given to validate the generation pointer "
            "against — ownership cannot be established"
        )
    if work_id != expected:
        raise CollectionRefused(
            f"generation pointer names work {work_id!r} — not this run's "
            f"{expected!r}; a foreign generation is never collection authority"
        )
    name = str(document.get("generation") or "")
    if not _GENERATION_NAME_RE.fullmatch(name):
        raise CollectionRefused(
            f"generation pointer names {name!r} — not the owned sibling shape "
            f"'{_GENERATION_PREFIX}<hex>'; an arbitrary path is never collection authority"
        )
    # The NAME is the authority: the generation is a DIRECT SIBLING of the
    # checkout (the regex already excludes separators, '..', and absolute
    # spellings), so the document's absolute convenience copy can only be
    # cross-checked, never followed.
    resolved = checkout_root.parent / name
    claimed = document.get("generation_path")
    if isinstance(claimed, str) and claimed.strip():
        claimed_path = Path(claimed.strip())
        if claimed_path.is_absolute():
            if os.path.realpath(claimed_path) != os.path.realpath(resolved):
                raise CollectionRefused(
                    f"generation pointer's absolute path {claimed!r} does not name "
                    f"the owned sibling {resolved} — refusing the foreign path"
                )
        elif claimed_path.name != name:
            raise CollectionRefused(
                f"generation pointer's relative path {claimed!r} does not name the "
                f"owned generation {name!r} — refusing the mismatched binding"
            )
    if not resolved.is_dir():
        raise CollectionRefused(
            f"generation pointer names {resolved}, which does not exist as a "
            "directory — the restore that minted it cannot have landed"
        )
    return resolved, work_id, str(document.get("checkpoint_id") or "")


def _clean_infrastructure(tree: Path) -> None:
    """Remove lane-infrastructure paths from *tree* before staging.

    The template step's own cleanup, mirrored INSIDE the collected tree:
    the top-level ``.codegraph`` / ``.venv`` / ``__pycache__`` /
    ``.pytest_cache`` directories and every ``*.pyc`` (the ``find
    . -name '*.pyc' -delete`` equivalent) — never a deliverable, in the
    generation as much as in the checkout.
    """
    for name in INFRASTRUCTURE_DIRS:
        shutil.rmtree(tree / name, ignore_errors=True)
    for directory, subdirs, files in os.walk(tree):
        # Never descend into .git — repository internals are not the
        # agent's work tree, and the walk is pure cost there.
        subdirs[:] = [name for name in subdirs if name != ".git"]
        for member in files:
            if member.endswith(".pyc"):
                Path(directory, member).unlink(missing_ok=True)


def collect_candidate(
    checkout_root: str | Path,
    expected_work_id: str,
    attempt_base_oid: str,
    output_root: str | Path | None = None,
    *,
    allow_missing_pointer: bool = False,
) -> CollectionResult:
    """Collect the candidate diff from the ACTIVE workspace generation.

    Resolution order (Q35-01):

    1. ``<checkout_root>/.forge/workspace-generation`` exists → its
       validated sibling generation is the collected tree (the resumed
       lane's contract). Validation is OWNERSHIP-shaped: work id, owned
       name, direct sibling, agreeing absolute path — see
       :func:`_resolve_pointer`. A refusal raises
       :class:`CollectionRefused` and produces ZERO artifacts.
    2. pointer absent + ``allow_missing_pointer`` → the fresh-run path:
       collect from *checkout_root* itself (the agent worked there; the
       commands and output contract are exactly the historical ones).
    3. pointer absent + not allowed → :class:`GenerationPointerMissing`.

    The frozen *attempt_base_oid* must be non-empty; staging and the
    ``--binary --full-index`` diff run via explicit ``git -C`` on the
    collected tree with NO error suppression — a non-zero Git exit is a
    :class:`CollectionError` carrying git's stderr, while a zero-change
    tree is a successful ``zero_change=True`` result.

    *output_root* defaults to ``<checkout_root>/forge-output`` (the
    emit-meta/upload contract path) and is cleared BEFORE staging, so
    stale candidate bytes can never ride into a later ``git add -A``.
    It may not be an ancestor of the collected tree, and for a
    generation collection it may not live INSIDE the generation — the
    diff is written outside the tree it describes.
    """
    checkout = Path(checkout_root).resolve()
    base_oid = str(attempt_base_oid or "").strip()
    if not base_oid:
        raise CollectionError("attempt base oid is empty — the diff has no frozen base")

    pointer_path = checkout / GENERATION_POINTER
    source = "checkout"
    checkpoint_id = ""
    if pointer_path.is_file():
        collected, work_id, checkpoint_id = _resolve_pointer(checkout, expected_work_id)
        source = "generation"
    elif allow_missing_pointer:
        collected = checkout
        work_id = (expected_work_id or "").strip()
    else:
        raise GenerationPointerMissing(
            f"{pointer_path} does not exist and a generation was required — "
            "the resumed candidate has no pointer to collect through"
        )

    _require_git_worktree(collected)

    output = (
        Path(output_root).resolve()
        if output_root is not None
        else checkout / DEFAULT_OUTPUT_DIRNAME
    )
    real_collected = Path(os.path.realpath(collected))
    real_output = Path(os.path.realpath(output))
    if real_collected == real_output or real_output in real_collected.parents:
        raise CollectionError(
            f"output root {output} contains the collected tree {collected} — the "
            "staging directory may never hold the worktree it collects"
        )
    if source == "generation" and (
        real_collected == real_output or real_collected in real_output.parents
    ):
        raise CollectionError(
            f"output root {output} is inside the collected generation {collected} — "
            "candidate bytes are written outside the worktree they describe"
        )

    # Clean the tree, then clear the staging directory — BOTH before the
    # first Git command, so neither infra paths nor stale candidate bytes
    # can reach the index.
    _clean_infrastructure(collected)
    shutil.rmtree(real_output, ignore_errors=True)

    staged = _run_git(collected, ("add", "-A"))
    if staged.returncode != 0:
        raise CollectionError(
            f"git add -A failed in {collected} (exit {staged.returncode}): "
            f"{staged.stderr.decode('utf-8', 'replace').strip()}"
        )
    diffed = _run_git(collected, ("diff", "--cached", "--binary", "--full-index", base_oid))
    if diffed.returncode != 0:
        raise CollectionError(
            f"git diff --cached against {base_oid} failed in {collected} "
            f"(exit {diffed.returncode}): "
            f"{diffed.stderr.decode('utf-8', 'replace').strip()}"
        )

    diff_bytes = diffed.stdout
    real_output.mkdir(parents=True, exist_ok=True)
    diff_path = real_output / CANDIDATE_DIFF_NAME
    diff_path.write_bytes(diff_bytes)
    return CollectionResult(
        diff_path=diff_path,
        diff_digest=hashlib.sha256(diff_bytes).hexdigest(),
        generation_path=str(real_collected),
        resolved_work_id=work_id,
        checkpoint_id=checkpoint_id,
        base_oid=base_oid,
        zero_change=not diff_bytes,
        source=source,
    )
