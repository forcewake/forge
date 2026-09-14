"""Reactive review lane (v0.7) — webhook-triggered PR reviews on GitHub.

This package is the GitHub reactive bot surface ported from the GitLab
reactive core (docs/research/github-reactive.md, feature F1). It is a
SEPARATE LANE beside the durable run lane (:mod:`forge.runs`):

- The durable lane (E1) is RunService/GitHubRunService: ``/implement`` →
  plan → human gate → publish → readonly review → ``ready_for_human``,
  driven by FlowRun rows and RunSpecs.
- The reactive lane has NO FlowRun, NO RunSpec and NO budget: a
  ``pull_request`` ``opened``/``synchronize`` webhook schedules one durable
  STEP (same inbox/step mechanics, ADR-0017) whose executor reviews the PR
  diff and posts a native GitHub review. Usage is still journaled in the
  ``llm_calls`` ledger by :class:`~forge.factory.llm.LLMClient`; there is
  simply no run to budget against.

Boundary rule (recursion guard): the reactive reviewer never reviews
forge's own output. Events from Bot senders, PRs authored by bots and
branches under the ``forge/`` prefix (E1's durable flow publishes forge
commits there) are skipped before any LLM call.
"""
