"""Complete bounded immutable tree differences; unsupported bytes refuse review."""

import difflib
import hashlib
import json

from forge.port_values import RepoRef, TreeEntry
from forge.ports import RepoPort
from forge.review.native_values import NativeReviewPolicy


class NativeDiffRefused(ValueError):
    pass


async def blob(
    repo: RepoPort, identity: RepoRef, sha: str, entry: TreeEntry | None, policy: NativeReviewPolicy
) -> bytes:
    if entry is None:
        return b""
    if entry.kind != "blob" or entry.mode not in ("100644", "100755", "120000"):
        raise NativeDiffRefused("native_review_unsupported_git_entry")
    if entry.size is not None and entry.size > policy.max_blob_bytes:
        raise NativeDiffRefused("native_review_blob_limit")
    value = await repo.read_blob(identity, sha, entry.path)
    if value is None or len(value) > policy.max_blob_bytes:
        raise NativeDiffRefused("native_review_blob_unavailable_or_large")
    raw = b"blob " + str(len(value)).encode() + b"\0" + value
    actual = (
        hashlib.sha1(raw).hexdigest() if len(entry.sha) == 40 else hashlib.sha256(raw).hexdigest()
    )
    if actual != entry.sha:
        raise NativeDiffRefused("native_review_blob_identity_mismatch")
    if b"\0" in value:
        raise NativeDiffRefused("native_review_binary_unsupported")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        raise NativeDiffRefused("native_review_binary_unsupported") from None
    return value


def text_delta(
    path: str, before: bytes, after: bytes, old: TreeEntry | None, new: TreeEntry | None
) -> bytes:
    label = json.dumps(path, ensure_ascii=False)
    header = f"File {label}: {old.mode if old else 'absent'} -> {new.mode if new else 'absent'}\n"
    lines = difflib.unified_diff(
        before.decode().splitlines(keepends=True),
        after.decode().splitlines(keepends=True),
        fromfile="before/" + path,
        tofile="after/" + path,
    )
    rendered = []
    for line in lines:
        rendered.append(line)
        if not line.endswith("\n"):
            rendered.append("\n\\ No newline at end of file\n")
    return header.encode() + "".join(rendered).encode()


async def native_diff(
    repo: RepoPort, identity: RepoRef, base: str, head: str, policy: NativeReviewPolicy
) -> bytes:
    trees = [await repo.read_tree(identity, sha) for sha in (base, head)]
    for tree, expected in zip(trees, (base, head), strict=True):
        if tree.repo.identity != identity.identity or tree.sha != expected:
            raise NativeDiffRefused("native_review_tree_identity_mismatch")
    before, after = (
        {entry.path: entry for entry in tree.entries if entry.kind != "tree"} for tree in trees
    )

    def identity_of(entry: TreeEntry | None) -> tuple[str, str, str] | None:
        return (entry.sha, entry.mode, entry.kind) if entry is not None else None

    changed = [
        path
        for path in sorted(before.keys() | after.keys())
        if identity_of(before.get(path)) != identity_of(after.get(path))
    ]
    if len(changed) > policy.max_files:
        raise NativeDiffRefused("native_review_file_limit")
    result, raw_size = bytearray(), 0
    for path in changed:
        if any(ord(char) < 32 for char in path):
            raise NativeDiffRefused("native_review_unsupported_path")
        old, new = before.get(path), after.get(path)
        old_bytes = await blob(repo, identity, base, old, policy)
        new_bytes = await blob(repo, identity, head, new, policy)
        raw_size += len(old_bytes) + len(new_bytes)
        if raw_size > policy.max_bytes:
            raise NativeDiffRefused("native_review_total_limit")
        result.extend(text_delta(path, old_bytes, new_bytes, old, new))
        if len(result) > policy.max_bytes:
            raise NativeDiffRefused("native_review_total_limit")
    return bytes(result)
