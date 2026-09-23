# Candidate collection from the active workspace generation (Q35-01)

Status: implemented. Issue: #238 (external review `c7ae8db`, item Q35-01).
Previous work: R32-01 (stable sibling generations), R32-17.

## The defect

A resumed lane restores its WIP into a stable SIBLING generation
(``<checkout-parent>/.forge-workspace-gen-<checkpoint_id[:12]>/``) and
``os.chdir()``s the lane process into it; the agent's edits land IN the
generation, while the checkout only gains the
``.forge/workspace-generation`` pointer. The shipped GitHub emit step ran
``git add -A`` + ``git diff --cached --binary --full-index <base>`` in the
ORIGINAL checkout (a fresh Actions shell — the lane's chdir does not
carry across steps), ending with ``|| true``. After a cross-runner resume
the uploaded candidate could be 0 bytes while the real work sat in the
generation, and a Git failure was indistinguishable from a no-op
candidate.

## The contract

One packaged collector — :mod:`forge.candidate_collector`
(``collect_candidate``), invoked by the emit step through
``python -m forge.harness_entry --collect-candidate``:

- **Resolution.** ``<checkout_root>/.forge/workspace-generation``
  (schema ``forge.workspace-generation/1``) decides the collected tree.
  The pointer's ``generation`` NAME is the authority: the collector
  resolves it as a direct sibling of the checkout and requires the owned
  shape ``.forge-workspace-gen-<hex>``. The document's absolute
  ``generation_path`` is only cross-checked (mismatch → refusal), never
  followed.
- **Ownership validation (fail-closed).** The pointer's ``work_id``
  must equal the expected work id (``--forge-run-id``); a forged shape,
  a foreign work id, an absolute path outside the sibling pattern, or a
  missing generation directory is a ``CollectionRefused`` with the
  reason and ZERO artifacts produced.
- **Fresh runs.** A missing pointer with
  ``allow_missing_pointer=True`` (the CLI default) collects the checkout
  itself — the classic no-resume flow, byte-compatible (same commands,
  same ``forge-output/candidate.diff`` path). ``--require-generation``
  pins the resumed-delivery profile: a missing pointer is then the typed
  ``GenerationPointerMissing``, never a fallback.
- **Real Git, explicit cwd.** ``git -C <validated-tree> add -A`` then
  ``git -C <validated-tree> diff --cached --binary --full-index <base>``.
  No ``|| true``: a non-zero Git exit is a ``CollectionError`` carrying
  git's stderr and fails the step. Unsupported layouts (a ``.git`` file
  — linked-worktree topology — or no ``.git`` at all) are detected
  before any command runs.
- **Zero-change is valid.** An empty diff with exit 0 is a successful
  result flagged ``zero_change=True`` — distinguishable from failure by
  type, never by byte-guessing.
- **Infrastructure exclusion.** ``.codegraph``, ``.venv``,
  ``__pycache__``, ``.pytest_cache`` and every ``*.pyc`` are removed
  from the COLLECTED tree before staging (the old step's cleanup list,
  applied where staging actually happens).
- **Output placement.** ``candidate.diff`` is written to the output root
  (default ``<checkout_root>/forge-output`` — the byte-for-byte
  ``--emit-meta``/upload contract), cleared BEFORE staging so stale
  candidate bytes can never enter a later ``git add -A``. The output
  root may never contain the collected tree, and for a generation
  collection may never live inside the generation.
- **Original checkout untouched.** On the resume path nothing is staged
  in the checkout: its index, worktree and pointer stay exactly as the
  lane left them; control metadata (``.forge/exit``, ``.forge/usage.json``,
  ``.forge/steering.json``) keeps its stable-checkout location while the
  candidate bytes derive from the actual working tree.

## Result shape

``CollectionResult`` carries: ``diff_path``, ``diff_digest`` (sha256 hex
over the exact bytes — the same binding the meta's ``manifest_digest``
carries), ``generation_path`` (absolute collected tree), the resolved
``work_id``/``checkpoint_id``, ``base_oid``, ``zero_change``, and
``source`` (``generation`` | ``checkout``). The CLI prints it as JSON
for the step log and exits non-zero on any ``CollectionError`` or
``CollectionRefused`` (the typed reason on stderr); it never rewrites
``.forge/exit`` — a collection failure reddens the job as infrastructure
and the turn's own classification stands.

## Rollout

The GitHub template (``ci/templates/forge-harness.github.yml``) and its
dogfood mirror (``.github/workflows/forge-harness.yml``) invoke the
collector. Other templates (Azure, GitLab SDK lanes) still carry inline
collector fragments — their migration is explicit (updating the
control-plane image does not update installed target templates).
