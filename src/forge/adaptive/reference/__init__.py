"""The REFERENCE package — forge's labelled evaluation/testing scenarios
(R37-19, issue #300, ADR-0031).

Three kinds of code live in this repository and a maintainer must be able
to tell them apart from the LAYOUT, not from reading every module:

- **product code** — ``src/forge`` minus this package: the runtime
  contracts and their owners (the boundary registry of ADR-0030). These
  decide;
- **fake-native integration** — THIS package: deterministic scenario
  builders and reference remotes that speak provider-shaped or
  wire-shaped contracts in-process (the SQLite twin, the native-shaped
  remote, the reactive scripted vendor). They prove the mechanisms the
  product contracts describe, cheaply and reproducibly — they are never
  selected by a runtime default and never deployed;
- **customer-executed evidence** — ``qualification/records`` and
  ``docs/releases/evidence``: measurements real deployments produced.

Every module here carries the reference label in its docstring, and the
membership rule is mechanical (``tests/test_reference_separation.py`` +
``tests/test_architecture_boundaries.py``):

1. runtime entry points (the provider services, ``main``, the wiring,
   the lane entry) must NOT import ``forge.adaptive.reference.*`` — the
   compat re-exports live only in the module each scenario was extracted
   from, so an old import path keeps working while callers drain;
2. this package composes runtime CONTRACTS only — it never imports a
   provider service, the wiring, ``main`` or the lane entry, and it adds
   no orchestration of its own;
3. importing a runtime module must never silently select a reference
   implementation: the compat homes that can stay reference-free at
   import time do (``saga_durable``, ``steering_causality``), and the one
   that executes a scenario by design (``system_verification`` — the
   twin's runner) names the reference module it pulls in.

The scenarios themselves are unchanged from their pre-extraction
behavior — this package is a labelling and dependency-direction change,
not a rewrite.
"""

from __future__ import annotations

__all__ = ["REFERENCE_PACKAGE_LABEL"]

#: The membership label every module in this package carries: reference
#: (evaluation/testing) material — deterministic scenario builders and
#: provider-shaped in-process remotes, never deployed guarantees.
REFERENCE_PACKAGE_LABEL = "forge.reference/1"
