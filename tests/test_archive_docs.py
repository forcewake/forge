"""Archival does not turn local historical material into current published proof."""

from pathlib import Path
import posixpath
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ARCHIVED_PREFIXES = (
    "qualification",
    "evaluation",
    "docs/evaluation",
    "docs/releases/evidence",
    "docs/reviews",
    "demo",
    "docs/roadmap/2026-09-21-adaptive",
)


def test_archive_identifies_a_local_unpublished_snapshot():
    text = (ROOT / "docs/archive.md").read_text()
    assert "archive/evidence-0.42" in text
    assert "7df3c46ddbf98d5967e976adce327403a34aed2a" in text
    assert "local only" in text and "has not been published" in text
    assert "Ignored" in text and "none were deleted or moved" in text
    assert not re.search(r"https?://\S*(?:blob|tree)/archive", text)


def test_current_markdown_has_no_links_into_the_archived_tree():
    tracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"], text=True)
    failures = []
    for name in tracked.split("\0"):
        path = ROOT / name
        if path.suffix.lower() not in {".md", ".markdown"} or not path.is_file():
            continue
        fence = None
        for number, line in enumerate(path.read_text().splitlines(), 1):
            marker = re.match(r"^\s*(```+|~~~+)", line)
            if marker:
                fence = marker[1][0] if fence is None else None
                continue
            if fence is not None:
                continue
            targets = re.findall(r"\]\((<[^>]+>|[^)\s]+)", line)
            targets += re.findall(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", line)
            for target in targets:
                url = urlsplit(target.strip("<>"))
                if url.scheme or url.netloc or not url.path:
                    continue
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), unquote(url.path))
                )
                if any(resolved == p or resolved.startswith(p + "/") for p in ARCHIVED_PREFIXES):
                    failures.append(f"{name}:{number}: {target}")
    assert not failures, failures
