"""Prove an observed native Git tree is exactly the validated publication."""

from collections.abc import Mapping
import hashlib
import re

from .candidate import ValidatedCandidate

GitLeaves = Mapping[str, tuple[str, str]]


def matches_manifest(candidate: ValidatedCandidate, before: GitLeaves, after: GitLeaves) -> bool:
    """Compare complete native leaf maps; callers must prove tree completeness."""
    paths = {entry.path for entry in candidate.manifest}
    if len(paths) != len(candidate.manifest) or not paths:
        return False
    if any(
        before.get(path) != after.get(path)
        for path in before.keys() | after.keys()
        if path not in paths
    ):
        return False
    for entry in candidate.manifest:
        original, observed = before.get(entry.path), after.get(entry.path)
        if entry.operation == "delete":
            if original is None or observed is not None:
                return False
            continue
        if (entry.operation == "create") != (original is None) or observed is None:
            return False
        mode, oid = observed
        body = entry.content_bytes
        if (
            body is None
            or mode != entry.mode
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid) is None
        ):
            return False
        hasher = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        expected = hasher(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        if oid != expected:
            return False
    return True
