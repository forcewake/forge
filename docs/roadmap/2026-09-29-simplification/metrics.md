# Simplification program — metrics

These are the targets from [DESIGN.md §12](DESIGN.md). From Phase 0 on,
`scripts/metrics.py` (P0-10) generates this file. Until then, the baseline
below was measured by hand with the method described in DESIGN §2.

| Metric | 2026-09-29 baseline (`7ed64ba`) | Phase 1 target | Phase 3 target | Phase 4 target |
| --- | --- | --- | --- | --- |
| `src/forge` lines | 190,383 (248 modules) | ≤ 110K | ≤ 75K | ≤ 75K |
| Runtime-reachable lines | 137,820 (201 modules) | — | — | — |
| Tooling-only lines | 33,082 (20 modules) | 0 in package | 0 | 0 |
| Test-only lines | 19,481 (27 modules) | 0 | 0 | 0 |
| Lines reachable from the lane entry points | 132,772 | — | `forge-lane` ≤ 8K | ≤ 8K |
| Largest file | 11,433 (`runs/service.py`) | unchanged | ≤ 1,000 | ≤ 1,000 |
| Functions > 100 / > 200 / > 400 lines | 234 / 56 / 8 | ≤ 200 | ≤ 20 | ≤ 20 |
| Prose share in `src` | ~28% | ≤ 18% | ≤ 12% | ≤ 12% |
| Review, issue and ADR references in `src` | 2,558 | falling | ≤ 50 (ADR refs only) | ≤ 50 |
| Direct runtime dependencies | 14 | ≤ 10 | ≤ 9 | ≤ 9 |
| Default test suite | 9,275 passed / 8 failed / 75 skipped, 16.5 min (full run, macOS) | ≤ 8 min, hermetic | ≤ 5 min | ≤ 5 min |
| Markdown words (active docs) | ~319K | ≤ 60K | ≤ 50K | ≤ 40K |
| First Draft PR on a fresh GitHub repository | not measured | — | ≤ 60 min | ≤ 30 min (non-owner) |

## Measured history

<!-- metrics:begin -->
| Metric | 2026-09-29 (`dae168c`) |
| --- | --- |
| `src/forge` lines | 190437 |
| Modules | 248 |
| Runtime-reachable lines | 137846 |
| Tooling-only lines | 33110 |
| Test-only lines | 19481 |
| Lines reachable from lane entry points | 132798 |
| Largest file | 11433 (src/forge/runs/service.py) |
| Functions > 80 lines | 322 |
| Functions > 100 lines | 234 |
| Functions > 200 lines | 56 |
| Prose share in `src` | 28% |
| Review and issue references in `src` | 2676 |
| ADR references in `src` | 705 |
| Direct runtime dependencies | 14 |
| Markdown words under `docs/` | 353249 |
| Default suite seconds | — |
<!-- metrics:end -->
