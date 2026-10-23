"""Project configuration is independent of the legacy orchestration package."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_project_config_reader_has_a_shared_home():
    module = importlib.import_module("forge.project_config")
    assert module.ProjectConfig.__module__ == "forge.project_config"
    assert module.ConfigReadResult.__module__ == "forge.project_config"
    assert callable(module.read_project_config)


def test_project_config_has_no_legacy_importers():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "src/forge/orchestrator/project_config.py").exists()
    old_name = "forge.orchestrator." + "project_config"
    remaining = [
        str(path.relative_to(root))
        for directory in ("src", "tests")
        for path in (root / directory).rglob("*.py")
        if old_name in path.read_text(encoding="utf-8")
    ]
    assert not remaining, remaining


@pytest.mark.parametrize("content", ["neighbors: wrong-shape\n", "neighbors: [1]\n"])
def test_malformed_neighbor_authorization_is_rejected(content):
    module = importlib.import_module("forge.project_config")
    with pytest.raises(ValueError, match="neighbors"):
        module._parse_project_config(content)


@pytest.mark.parametrize("status", ["forbidden", "unavailable", "incomplete"])
async def test_shared_config_reader_preserves_failed_authoritative_reads(status):
    from forge.gitlab.blob_reads import BlobReadResult
    from forge.project_config import read_project_config, clear_cache

    class Reader:
        async def read_blob(self, project_id, path, ref):
            return BlobReadResult(status=status, detail="provider read failed")

    clear_cache()
    result = await read_project_config(Reader(), 42, "a" * 40)
    assert result.status == "unreadable" and result.needs_block
    assert result.config is None


async def test_shared_config_reader_retains_invalid_restrictions_instead_of_defaults():
    from forge.gitlab.blob_reads import BlobReadResult
    from forge.project_config import read_project_config, clear_cache

    class Reader:
        async def read_blob(self, project_id, path, ref):
            return BlobReadResult.found("neighbors: [1]\n")

    clear_cache()
    result = await read_project_config(Reader(), 42, "a" * 40)
    assert result.status == "invalid" and result.needs_block
    assert result.config is None
