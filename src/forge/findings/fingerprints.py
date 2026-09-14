"""Fingerprint computation — the dedupe key per provider (research §3/§4).

GitLab: forge computes the fingerprint itself. The report schema carries a
scanner-assigned ``id`` (UUID) that is NOT stable across runs for all
scanners, and CE users get none of GitLab Ultimate's internal
location/track fingerprints — the report JSON has no fingerprint column at
all. Per research §4.1 the rule is: sha256 over ``category`` + primary
``identifiers[].value`` + the location hash. The primary identifier is the
first identifier whose value looks like a CVE/CWE/GSA reference, else the
first identifier, else the finding name (secret detection reports often
carry a single named identifier).

GitHub: the alert number per repository — server-assigned, stable, and the
exact handle the PATCH-dismiss endpoints take (research §3.2/§4.2).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_RE_ID_PREFIXES = ("CVE-", "CWE-", "GHSA-", "GOSEC-")


def gitlab_fingerprint(
    *,
    category: str,
    identifiers: list[dict[str, Any]] | None,
    location: dict[str, Any] | None,
    fallback_key: str = "",
) -> str:
    """sha256 over category + primary identifier value + canonical location.

    *location* is the category-specific location object (SAST: ``{file,
    start_line, ...}``; dependency scanning: ``{file, dependency{...}}``) —
    it is canonicalized via sorted-key JSON so the hash is stable across
    runs of the same scanner version. *fallback_key* (usually the finding
    name) keeps degenerate reports without identifiers dedupable instead
    of colliding on an empty identity.
    """
    primary = _primary_identifier_value(identifiers)
    location_part = json.dumps(location or {}, sort_keys=True, separators=(",", ":"))
    material = "|".join((category or "", primary, location_part, fallback_key or ""))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _primary_identifier_value(identifiers: list[dict[str, Any]] | None) -> str:
    """The strongest stable identifier value: CVE/CWE/GHSA first, else first."""
    for ident in identifiers or []:
        value = str(ident.get("value") or "")
        if value and value.upper().startswith(_RE_ID_PREFIXES):
            return value
    for ident in identifiers or []:
        value = str(ident.get("value") or "")
        if value:
            return value
    return ""


def github_fingerprint(alert_number: int | str) -> str:
    """The GitHub alert number, normalized to a string (research §3.2)."""
    return str(alert_number)
