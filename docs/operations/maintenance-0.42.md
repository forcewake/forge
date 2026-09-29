# Maintenance line 0.42.x

ADR-0035 moves Azure DevOps and the pre-simplification architecture to a
maintenance line while `main` is simplified.

| Item | Value |
| --- | --- |
| Branch | `release/0.42.x`, cut from `7ed64ba`: the `v0.42.0` tag plus its release evidence and lane pins |
| Recovery tag | `pre-simplification`: the last `main` commit before the Phase 1 deletions. It is created at the Phase 0 exit. |
| Scope | Security fixes only; no features |
| Azure DevOps | Supported only on this line once it leaves `main` (ROADMAP P1-11), until the post-1.0 port adapter lands |
| CI | `ci.yml` runs on pushes to `release/**` and on pull requests against it. The nightly-only jobs (`integration-os`, `release-canary`) never run on this branch by schedule; dispatch them before a release. |
| Releases | `v0.42.N` tags from the branch, through that branch's own release workflow and runbook |

## Creating the branch (owner)

GitHub reads a workflow's triggers from the pushed commit, so the
`release/**` trigger must be on the branch itself:

```bash
git fetch origin --tags
git switch -c release/0.42.x 7ed64ba
git cherry-pick -x <main sha of "docs(ops): the 0.42.x maintenance line; CI runs on release branches">
git push -u origin release/0.42.x
```

## Backporting a fix

```bash
git switch -c backport/<topic> origin/release/0.42.x
git cherry-pick -x <sha>       # or commit the fix directly when the code is already gone from main
gh pr create --base release/0.42.x
```

## Releasing 0.42.N

The branch keeps the 0.42 release machinery, so follow its own
`docs/releases/README.md`:

1. Bump the version.
2. Record the qualification evidence that its exact-composition gate
   requires.
3. Run the nightly-only jobs with `gh workflow run ci.yml --ref release/0.42.x`.
4. Tag the exact commit whose CI run finished green.

Known follow-up before the first `0.42.N` release after `0.43.0` ships: the
branch's `release.yml` always moves `ghcr.io/forcewake/forge:latest` and
canaries an upgrade from it. On the branch, change it to tag only
`:0.42.N` and to canary against the previous `0.42.x` image.

## Recovering removed code

Once the `pre-simplification` tag exists, code that is later removed from
`main` can be read or restored from it:

```bash
git fetch origin tag pre-simplification
git show pre-simplification:src/forge/runs/azure_service.py
git restore --source=pre-simplification -- src/forge/runs/azure_service.py
```

Fixes made on `main` after the tag exist only in `main`'s history.
