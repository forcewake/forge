# Maintenance line 0.42.x

ADR-0035 moves Azure DevOps and the pre-simplification architecture to a
maintenance line while `main` is simplified.

| Item | Value |
| --- | --- |
| Branch | `release/0.42.x`, cut from `7ed64ba`: the `v0.42.0` tag plus its release evidence and lane pins |
| Recovery tag | `pre-simplification`: the last `main` commit before the Phase 1 deletions. It is created at the Phase 0 exit. |
| Scope | Security fixes only; no features |
| Azure DevOps | Supported only on this line once it leaves `main` (ROADMAP P1-11), until the post-1.0 port adapter lands |
| CI | `ci.yml` runs on pushes to `release/**` and on pull requests against it. The schedule-only jobs (`integration-os`, `release-canary`) do not run here. They are advisory, not promotion-required, and were already failing at the branch point. |
| Releases | None planned. The line exists for security fixes, in case an installation still depends on it. |

## Creating the branch (owner, after Phase 0 merges)

GitHub reads a workflow's triggers from the pushed commit, so the branch
needs the `release/**` trigger itself. Cherry-pick only the trigger commit,
because later `ci.yml` changes on `main` depend on files the branch does
not have. Then take the current version of this policy:

```bash
git fetch origin --tags
git switch -c release/0.42.x 7ed64ba
git cherry-pick -x <main sha of "docs(ops): the 0.42.x maintenance line; CI runs on release branches">
git checkout origin/main -- docs/operations/maintenance-0.42.md
git commit -m "docs(ops): the current maintenance policy"
git push -u origin release/0.42.x
```

Expected: the first push run of `ci.yml` on the branch is green. The push
run on `7ed64ba` was green.

## Backporting a fix

```bash
git fetch origin
git switch -c backport/<topic> origin/release/0.42.x
git cherry-pick -x <sha>       # or commit the fix directly when the code is already gone from main
gh pr create --base release/0.42.x
```

## If a 0.42.N release becomes necessary

The branch keeps the 0.42 release machinery, and it is documented on the
branch:

- **The promotion gate and evidence:** `docs/releases/README.md`.
- **Before the release:** the version bump (`src/forge/__init__.py` and
  `pyproject.toml`), the qualification record and the wheel receipt that
  the exact-composition gate requires. See `docs/releases/profile-records.md`
  and `docs/operations/supported-profile-runbook.md` §9.
- **After the release:** commit `docs/releases/evidence/v0.42.N/` and
  regenerate the lane pins with `scripts/generate_template_pins.py`, as
  `7ed64ba` did for 0.42.0.

Tag only a commit whose push run is green, and do not dispatch extra runs
on that commit. The promotion gate reads the latest attempt of each
required check on that SHA.

Known follow-ups on the branch before any 0.42.N release after 0.43.0 ships:

- `release.yml` moves `ghcr.io/forcewake/forge:latest` and canaries an
  upgrade from it. Change it to tag only `:0.42.N` and to canary against
  the previous `0.42.x` image.
- `release.yml` creates the GitHub Release without `--latest=false`.
- The `release-canary` job in `ci.yml` hard-codes
  `--previous-image ghcr.io/forcewake/forge:latest`.
- `release-canary` and `integration-os` were already failing on the nightly
  schedule at the branch point. Repair them before relying on them.

## Recovering removed code

Once the `pre-simplification` tag exists, code that is later removed from
`main` can be read or restored from it:

```bash
git fetch origin tag pre-simplification
git show pre-simplification:src/forge/runs/azure_service.py
git restore --source=pre-simplification -- src/forge/runs/azure_service.py
```

Fixes made on `main` after the tag exist only in `main`'s history.
