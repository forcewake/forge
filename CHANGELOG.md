# Changelog

Changes use [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This source tree is unreleased; no version or publication is declared here.

## [Unreleased]

### Changed

- Simplify the server around the validated publisher, durable approval and provider-native verification contracts.
- Separate versioned protocol models and the CI runner into workspace packages.
- Build release artifacts once and preserve their digests through verification and promotion.
- Keep GitHub and GitLab active; preserve Azure DevOps on the 0.42.x maintenance line.
- Remove legacy, deferred and qualification-only runtime surfaces while retaining their safety lessons and tests.
- Reduce active documentation and preserve obsolete material in local archive snapshots.

The [implementation record](docs/roadmap/2026-09-29-simplification/IMPLEMENTATION-1.0.md)
states task-level progress and remaining checks. Full historical release
entries remain unchanged in [the 0.x changelog](docs/archive/CHANGELOG-0.x.md).
