"""DSC-04: the project map separates observed facts from labeled guesses.

The tests prove the map's three contracts: every bucket is a practical
detection over the snapshot; generated/vendor trees are excluded by
declared policy and never reach a bucket; and provenance is honest —
facts carry citations, inferences carry confidence, discovered commands
stay unvalidated metadata.
"""

from __future__ import annotations

import pytest

from forge.adaptive.project_map import (
    commands_as_metadata,
    extract_project_map,
    fact_with_citation,
    inferred_relation,
)

_SNAPSHOT = {
    "pyproject.toml": "[project]\nname = 'demo'\n",
    "requirements.txt": "httpx\n",
    "package.json": '{"name": "web"}\n',
    "src/package.json": '{"name": "nested-web"}\n',
    ".github/workflows/ci.yml": (
        "on: [push]\njobs:\n  test:\n    steps:\n      - run: pytest -q\n"
    ),
    ".gitlab-ci.yml": "test:\n  script:\n    - make test\n",
    "api/routes.py": ("def list_users():\n    ...\n\n\nclass UsersRouter:\n    ...\n"),
    "src/router_helpers.py": "def wire_router():\n    ...\n",
    "events/user_created.json": '{"type": "user_created"}\n',
    "schemas/event_order.avro": "avro-schema\n",
    "migrations/0001_init.py": "def upgrade(): pass\n",
    "alembic/versions/002_add_email.py": "def upgrade(): pass\n",
    "tests/test_demo.py": "def test_demo(): pass\n",
    "node_modules/left-pad/package.json": '{"name": "left-pad"}\n',
    "vendor/lib/util.py": "def helper(): pass\n",
    "app/README.md": "# demo\n",
}


class TestExtractProjectMap:
    def test_the_schema_tag_is_forge_project_map_1(self):
        assert extract_project_map(_SNAPSHOT)["schema"] == "forge.project-map/1"

    def test_manifests_are_root_dependency_files_plus_nested_project_files(self):
        manifests = extract_project_map(_SNAPSHOT)["manifests"]
        assert "pyproject.toml" in manifests
        assert "requirements.txt" in manifests
        assert "package.json" in manifests
        assert "src/package.json" in manifests
        assert "app/README.md" not in manifests

    def test_ci_definitions_cover_github_gitlab_and_azure(self):
        ci = extract_project_map(_SNAPSHOT)["ci_definitions"]
        assert ".github/workflows/ci.yml" in ci
        assert ".gitlab-ci.yml" in ci

    def test_public_api_entries_carry_confidence_and_kind(self):
        public_api = extract_project_map(_SNAPSHOT)["public_api"]
        by_symbol = {(entry["symbol"], entry["kind"]) for entry in public_api}
        assert ("list_users", "function") in by_symbol
        assert ("UsersRouter", "class") in by_symbol
        assert ("wire_router", "function") in by_symbol

        api_entry = next(e for e in public_api if e["symbol"] == "list_users")
        name_entry = next(e for e in public_api if e["symbol"] == "wire_router")
        # The api/ tree is the stronger signal; a router-ish FILENAME the weaker.
        assert 0.0 < name_entry["confidence"] < api_entry["confidence"] <= 1.0
        assert api_entry["line_no"] == 1

    def test_event_schemas_are_event_named_data_files(self):
        events = extract_project_map(_SNAPSHOT)["event_schemas"]
        assert "events/user_created.json" in events
        assert "schemas/event_order.avro" in events

    def test_migrations_cover_both_layouts(self):
        migrations = extract_project_map(_SNAPSHOT)["migrations"]
        assert "migrations/0001_init.py" in migrations
        assert "alembic/versions/002_add_email.py" in migrations

    def test_test_locations_cover_named_files_and_tests_dirs(self):
        locations = extract_project_map(_SNAPSHOT)["test_locations"]
        assert locations == ["tests/test_demo.py"]

    def test_generated_and_vendor_paths_are_excluded_from_every_bucket(self):
        project_map = extract_project_map(_SNAPSHOT)
        assert set(project_map["generated_excluded"]) == {
            "node_modules/left-pad/package.json",
            "vendor/lib/util.py",
        }
        for bucket in (
            "manifests",
            "ci_definitions",
            "event_schemas",
            "migrations",
            "test_locations",
        ):
            assert not [p for p in project_map[bucket] if "node_modules/" in p or "vendor/" in p]
        assert not [entry for entry in project_map["public_api"] if "vendor/" in entry["path"]]

    def test_a_repository_without_tests_lands_in_coverage_gaps(self):
        project_map = extract_project_map({"main.py": "print('hi')\n", "pyproject.toml": ""})
        assert project_map["test_locations"] == []
        assert len(project_map["coverage_gaps"]) == 1
        assert project_map["coverage_gaps"][0]["reason"] == "no test locations discovered"

    def test_a_repository_with_tests_has_no_coverage_gaps(self):
        assert extract_project_map(_SNAPSHOT)["coverage_gaps"] == []


class TestFactsAndRelations:
    def test_observed_facts_carry_their_citation(self):
        fact = fact_with_citation(
            "ci_definition",
            ".github/workflows/ci.yml",
            repository_id="repo-a",
            source_oid="1" * 40,
            path=".github/workflows/ci.yml",
            line_no=3,
        )
        assert fact == {
            "kind": "ci_definition",
            "value": ".github/workflows/ci.yml",
            "repository_id": "repo-a",
            "source_oid": "1" * 40,
            "path": ".github/workflows/ci.yml",
            "line_no": 3,
        }

    def test_line_number_is_optional(self):
        fact = fact_with_citation(
            "manifest", "pyproject.toml", "repo-a", "1" * 40, "pyproject.toml"
        )
        assert fact["line_no"] is None

    def test_observed_facts_never_carry_confidence(self):
        fact = fact_with_citation(
            "manifest", "pyproject.toml", "repo-a", "1" * 40, "pyproject.toml"
        )
        assert "confidence" not in fact

    def test_inferred_relations_carry_confidence(self):
        relation = inferred_relation(
            "api_backed_by_schema", "api/routes.py", "events/user_created.json", 0.6
        )
        assert relation == {
            "kind": "api_backed_by_schema",
            "source": "api/routes.py",
            "target": "events/user_created.json",
            "confidence": 0.6,
        }

    def test_confidence_out_of_range_is_refused(self):
        with pytest.raises(ValueError, match="0.0-1.0"):
            inferred_relation("relates", "a", "b", 1.5)
        with pytest.raises(ValueError, match="0.0-1.0"):
            inferred_relation("relates", "a", "b", -0.1)


class TestCommandsAsMetadata:
    def test_makefile_targets_become_make_commands(self):
        files = {
            "Makefile": (
                "VERSION := 1.0\n.PHONY: test\ntest:\n\tpytest -q\n\nbuild: test\n\tuvs build\n"
            )
        }
        commands = commands_as_metadata(files)
        assert {"command": "make test", "source_path": "Makefile", "validated": False} in commands
        assert {"command": "make build", "source_path": "Makefile", "validated": False} in commands
        # Variable assignments and dot-specials are not targets.
        assert {entry["command"] for entry in commands} == {"make test", "make build"}

    def test_ci_script_commands_are_extracted(self):
        commands = commands_as_metadata(_SNAPSHOT)
        rendered = [(entry["command"], entry["source_path"]) for entry in commands]
        assert ("pytest -q", ".github/workflows/ci.yml") in rendered
        assert ("make test", ".gitlab-ci.yml") in rendered

    def test_every_discovered_command_stays_unvalidated(self):
        commands = commands_as_metadata(_SNAPSHOT)
        assert commands
        assert all(entry["validated"] is False for entry in commands)
        assert all(set(entry) == {"command", "source_path", "validated"} for entry in commands)

    def test_vendored_makefiles_are_not_our_commands(self):
        files = {"vendor/tool/Makefile": "sneaky:\n\trm -rf /\n"}
        assert commands_as_metadata(files) == []

    def test_malformed_ci_yaml_yields_nothing_without_failing(self):
        files = {".github/workflows/broken.yml": "on: [push\n  bad: - }\n"}
        assert commands_as_metadata(files) == []
