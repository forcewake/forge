"""Local documentation links resolve against the checkout, including heading anchors."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import posixpath
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def prose(text):
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip()
            ):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def destinations(text):
    # Angle targets permit spaces; ordinary destinations balance their own parentheses.
    pattern = r"\]\(\s*(?:<([^>]+)>|((?:[^\s()]|\([^()]*\))+))(?:\s+[\"\'][^\"\']*[\"\'])?\s*\)"
    content = re.sub(r"(`+).*?\1", "", prose(text))
    targets = [a or b for a, b in re.findall(pattern, content)]
    targets += [
        a or b for a, b in re.findall(r"^\s*\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))", content, re.M)
    ]
    targets += re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", content)
    return targets


def anchors(text):
    result, counts = set(), Counter()
    content = prose(text)
    for heading in re.findall(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", content, re.M):
        slug = re.sub(r"[^\w\- ]", "", re.sub(r"<[^>]+>", "", heading).lower()).replace(" ", "-")
        index = counts[slug]
        result.add(slug + (f"-{index}" if index else ""))
        counts[slug] += 1
    result.update(re.findall(r"(?:id|name)=[\"']([^\"']+)[\"']", content))
    return result


def broken_links(root, files):
    failures = []
    for name in files:
        path = root / name
        if path.suffix.lower() not in {".md", ".markdown"} or not path.is_file():
            continue
        for target in destinations(path.read_text()):
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            relative = (
                posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(url.path)))
                if url.path
                else name
            )
            if url.path.startswith("/"):
                relative = url.path.lstrip("/")
            destination = root / relative
            if not destination.exists():
                failures.append(f"{name}: {target} (missing path)")
            elif url.fragment and destination.suffix.lower() in {".md", ".markdown"}:
                if unquote(url.fragment) not in anchors(destination.read_text()):
                    failures.append(f"{name}: {target} (missing heading)")
    return failures


def test_active_markdown_links_resolve():
    files = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"], text=True).split(
        "\0"
    )
    # The moved changelog is immutable history with its original relative links.
    files = [name for name in files if name != "docs/archive/CHANGELOG-0.x.md"]
    assert not (failures := broken_links(ROOT, files)), "\n".join(failures)


def test_link_checker_catches_missing_files_and_headings(tmp_path):
    (tmp_path / "README.md").write_text(
        "[missing](gone.md) [bad](guide.md#absent)\n```\n[example](ignored.md)\n```\n"
    )
    (tmp_path / "guide.md").write_text("# Existing heading\n")
    failures = broken_links(tmp_path, ["README.md"])
    assert (
        len(failures) == 2
        and any("missing path" in row for row in failures)
        and any("missing heading" in row for row in failures)
    )
    (tmp_path / "README.md").write_text("[valid](guide.md#existing-heading)\n")
    assert not broken_links(tmp_path, ["README.md"])


def test_documentation_budget_keeps_history_explicit():
    counts = {
        str(path.relative_to(ROOT)): len(path.read_text().split())
        for path in (ROOT / "docs").rglob("*.md")
    }
    active = {
        name: value
        for name, value in counts.items()
        if not name.startswith("docs/roadmap/2026-09-29-simplification/")
        and name != "docs/archive/CHANGELOG-0.x.md"
    }
    assert "docs/reference/edge-cases.md" in active
    assert all(name in active for name in counts if name.startswith("docs/adr/"))
    assert sum(active.values()) <= 60000
    assert sum(counts.values()) > sum(active.values())
    assert len((ROOT / "README.md").read_text().split()) <= 900


def test_historical_changelog_keeps_exact_snapshot_bytes():
    manifest = json.loads((ROOT / "docs/archive/documentation-0.42.json").read_text())
    original = next(row for row in manifest["rows"] if row["path"] == "CHANGELOG.md")
    history = ROOT / "docs/archive/CHANGELOG-0.x.md"
    assert hashlib.sha256(history.read_bytes()).hexdigest() == original["sha256"]
    assert manifest["snapshot"] == "a80706e54674b5abb571f4b8481252a41273c0c7"
    assert manifest["local_ref"] == "archive/docs-0.42-20261023-a80706e"
    assert manifest["published"] is False
