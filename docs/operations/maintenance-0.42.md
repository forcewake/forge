# Maintenance line 0.42.x

ADR-0035 moves Azure DevOps and the pre-simplification architecture to a
maintenance line while `main` is simplified.

| Item | Value |
| --- | --- |
| Branch | `release/0.42.x`, cut from tag `v0.42.0` |
| Recovery tag | `pre-simplification`: the last `main` commit before Phase 1 deletions |
| Scope | Security fixes and data-loss fixes only; no features |
| Azure DevOps | Supported only on this line until the post-1.0 port adapter lands |
| CI | The full `ci.yml` runs on every push to `release/**` |
| Releases | `v0.42.N` tags from the branch, through the existing release workflow |

## Backporting a fix

1. Fix on `main` first when the code still exists there; otherwise fix on the
   branch directly.
2. Cherry-pick onto `release/0.42.x` in a pull request against that branch.
3. Tag `v0.42.N` after CI is green.

## Recovering removed code

Every capability deleted from `main` during the simplification program is
recoverable from the tag `pre-simplification`:

```bash
git show pre-simplification:src/forge/runs/azure_service.py
```
