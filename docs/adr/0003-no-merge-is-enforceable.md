# 0003 — No-merge is enforceable, not prompt-only

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

An LLM-driven bot that is *capable* of merging is one prompt injection away
from shipping unreviewed code. Instructions in a system prompt ("never
merge") are not a security control: they can be overridden by hostile content
in issues, logs, or tool results, and they cannot be audited the way
permissions can.

Two facts about GitLab permissions must be respected:

- The Developer role alone does not prevent merging; whether a Developer can
  merge depends on protected-branch rules.
- A "no merge in code" claim is only credible if the merge capability does
  not exist in the write path at all.

## Decision

The no-merge rule is enforced structurally, in layers:

1. **Executor capability.** The forge executor does not implement merge,
   auto-merge, or writes to protected target branches. The functions do not
   exist; there is nothing for a prompt to persuade.
2. **Model scope.** The LLM never receives GitLab credentials or unrestricted
   write tools. All writes go through the trusted validator/executor
   ([ADR-0001](0001-commits-api-write-backend-changeset-contract.md)).
3. **GitLab permissions.** The practical CE profile is: the bot account holds
   the Developer role in the project; target branches are protected with
   "Allowed to merge" set to Maintainers only; direct pushes to those
   branches are forbidden. Overlapping protected-branch rules must be
   checked during onboarding, and the resulting constraints for human users
   must be agreed with the project's owners.

The iron principle: **the bot never merges.** A human reviews and merges
every change in GitLab.

## Consequences

- **Positive:** merging requires a human with Maintainer rights on a
  protected branch; the bot account is structurally incapable of merging;
  the control is auditable in GitLab settings, not in prompts.
- **Negative:** correctness depends on GitLab being configured correctly —
  onboarding must verify protected-branch rules, and drift must be detected
  (the onboarding `doctor` re-checks them). Branches that are not protected
  are outside this control's scope; forge targets protected branches for its
  work.
