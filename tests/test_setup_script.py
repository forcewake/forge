from unittest.mock import MagicMock, patch

import httpx
import pytest

from scripts.setup_gitlab import main


@pytest.fixture()
def mock_settings():
    """Patch Settings to avoid needing a real .env file."""
    with patch("scripts.setup_gitlab.Settings") as mock_cls:
        settings = MagicMock()
        settings.GITLAB_URL = "https://gitlab.test"
        settings.GITLAB_TOKEN.get_secret_value.return_value = "glpat-test"
        settings.GITLAB_WEBHOOK_SECRET.get_secret_value.return_value = "test-secret"
        mock_cls.return_value = settings
        yield settings


@pytest.fixture()
def mock_client():
    """Create a mock httpx.Client."""
    with patch("scripts.setup_gitlab._api_client") as mock_fn:
        client = MagicMock(spec=httpx.Client)
        mock_fn.return_value = client
        yield client


def test_dry_run_no_api_calls(mock_settings, mock_client, capsys):
    """Dry-run should print plan but not register webhooks."""
    # Mock connectivity check
    version_resp = MagicMock()
    version_resp.json.return_value = {"version": "18.0.0"}

    # Mock list hooks (empty)
    hooks_resp = MagicMock()
    hooks_resp.json.return_value = []

    mock_client.get.side_effect = [version_resp, hooks_resp]

    result = main(["--project-id", "42", "--webhook-url", "http://forge:8420/webhook", "--dry-run"])

    assert result == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "would register" in output

    # No POST calls should be made in dry-run
    mock_client.post.assert_not_called()


def test_duplicate_webhook_skipped(mock_settings, mock_client, capsys):
    """Existing webhook with same URL should be skipped."""
    version_resp = MagicMock()
    version_resp.json.return_value = {"version": "18.0.0"}

    hooks_resp = MagicMock()
    hooks_resp.json.return_value = [{"id": 99, "url": "http://forge:8420/webhook"}]

    # Mock labels POST (409 = already exists)
    label_resp = MagicMock()
    label_resp.status_code = 409

    mock_client.get.side_effect = [version_resp, hooks_resp]
    mock_client.post.return_value = label_resp

    result = main(["--project-id", "42", "--webhook-url", "http://forge:8420/webhook"])

    assert result == 0
    output = capsys.readouterr().out
    assert "already registered" in output


def test_successful_registration(mock_settings, mock_client, capsys):
    """Successful webhook registration flow."""
    version_resp = MagicMock()
    version_resp.json.return_value = {"version": "18.0.0"}

    hooks_resp = MagicMock()
    hooks_resp.json.return_value = []  # no existing hooks

    hook_created_resp = MagicMock()
    hook_created_resp.json.return_value = {"id": 123}

    label_resp = MagicMock()
    label_resp.status_code = 201

    mock_client.get.side_effect = [version_resp, hooks_resp]
    mock_client.post.side_effect = [hook_created_resp, label_resp, label_resp, label_resp]

    result = main(["--project-id", "42", "--webhook-url", "http://forge:8420/webhook"])

    assert result == 0
    output = capsys.readouterr().out
    assert "webhook registered (hook 123)" in output


def test_no_project_or_group_fails(mock_settings):
    """Missing both --project-id and --group-id should error."""
    with pytest.raises(SystemExit):
        main([])
