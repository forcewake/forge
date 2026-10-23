"""Stable native command identities replace legacy mention fingerprints."""

from forge.worker.steps import command_source_event_id
from forge.gateway.github_webhook import github_source_event_id


def test_delivery_identity_is_deterministic():
    assert command_source_event_id("status", 42, 7) == command_source_event_id("status", 42, "7")


def test_different_commands_never_share_a_delivery():
    assert command_source_event_id("status", 42, 7) != command_source_event_id("go", 42, 7)


def test_different_native_notes_never_share_a_delivery():
    assert command_source_event_id("status", 42, 7) != command_source_event_id("status", 42, 8)


def test_different_projects_never_share_a_delivery():
    assert command_source_event_id("status", 42, 7) != command_source_event_id("status", 43, 7)


def test_same_native_numbers_on_other_connections_are_distinct():
    left = github_source_event_id("github:1:acme/app", "issue_comment", "created", "comment:7")
    right = github_source_event_id("github:2:acme/app", "issue_comment", "created", "comment:7")
    assert left != right
