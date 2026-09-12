# 0002 — GitLab CI is the execution environment, but security comes from explicit execution profiles

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge validates proposed changes by running pipelines. That means repository
content — including content produced by an LLM — is executed by GitLab CI.
GitLab itself documents CI as a remote-code-execution surface: it warns about
shared and persistent runners, job-secret leakage, and states that shell
executors are intended for trusted builds. The mere existence of a
`.gitlab-ci.yml` is not a security boundary.

Additional constraints:

- The first push can already trigger CI, so the inventory of runner
  configurations, protected variables, and job permissions must be examined
  **before** the first commit, not after an incident.
- Banning agent edits to `.gitlab-ci.yml` is necessary but not sufficient:
  tests, build scripts, and package-manager hooks also execute arbitrary
  code.
- Agent code must not get access to production secrets, control-plane
  credentials, the Docker socket, privileged execution, or unrestricted
  access to internal infrastructure.

## Decision

CI is where candidate changes are executed and verified, but security is
provided by an explicit **execution profile** per project, approved during
onboarding before the first forge-driven commit:

- The agent's execution environment receives no production secrets, no
  control-plane credentials, no Docker socket, no privileged containers, and
  no unrestricted network access to internal infrastructure.
- CI jobs run in isolated, ephemeral environments with resource and network
  limits.
- Access to package mirrors and any test services required by the pipeline is
  enumerated explicitly in the profile.
- A shell-executor runner is acceptable only as a one-off laboratory with
  trusted fixtures and no secrets. It is never the recommended configuration
  for pipelines that execute arbitrary LLM output.

Projects that cannot provide an approved execution profile are not onboarded;
they get `blocked`, not a simulated green result.

## Consequences

- **Positive:** the security posture of CI execution is reviewable, auditable,
  and explicit per project; a compromised job has narrowly scoped access; the
  profile is re-examined whenever CI configuration, runners, or CI variables
  change.
- **Negative:** onboarding requires an inventory step and operator approval;
  some repositories cannot be onboarded until a safe profile exists — this is
  a deliberate trade-off against silent exposure.
- No-merge ([ADR-0003](0003-no-merge-is-enforceable.md)) is **not** a
  compensating control for a compromised CI: if CI is compromised, onboarding
  is blocked regardless.
