"""Runtime configuration is closed and cannot restore old authority fallbacks."""

import json
import subprocess
import sys

from pydantic import ValidationError
import pytest

from forge.runtime.authority import Operators, NotAuthorized
from forge.runtime.settings import RuntimeSettings, load, secret
from tests.runtime_fixture import configuration, OPERATOR_TOKEN


def document(config):
    return config.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(legacy_run_mode=True),
        lambda data: data["connections"][0].update(app_issuer="forbidden-global-app"),
        lambda data: data["connections"][0]["repositories"][0].update(read_token_env=None),
        lambda data: data["connections"][0]["repositories"].append(
            data["connections"][0]["repositories"][0].copy()
        ),
        lambda data: data["operators"][0]["grants"][0].update(repository="other"),
        lambda data: data["connections"][0]["repositories"][0].update(
            neighbors=[{"repository": "unconfigured"}]
        ),
        lambda data: data["connections"][0]["repositories"][0]["policy"].update(
            driver="uninstalled_legacy_cli"
        ),
        lambda data: data["connections"][0].update(oidc_issuer="http://untrusted.invalid"),
    ],
)
def test_closed_settings_reject_unknown_fallbacks_unbound_native_authority_and_drivers(
    tmp_path, mutate
):
    config, _ = configuration(tmp_path)
    value = document(config)
    mutate(value)
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(value))


def test_secret_resolution_has_only_explicit_environment_names_and_never_echoes_values(tmp_path):
    config, values = configuration(tmp_path)
    assert secret(config.secret_key_env, values).get_secret_value() == values[config.secret_key_env]
    with pytest.raises(ValueError, match="SYNTHETIC_SECRET") as error:
        secret("SYNTHETIC_SECRET", {"FORGE_SECRET_KEY": "owner-value-must-not-be-read"}, minimum=32)
    assert "owner-value" not in str(error.value)
    assert values["SYNTHETIC_SECRET"] not in repr(secret(config.secret_key_env, values))


def test_operator_grants_produce_only_configured_native_human_identity(tmp_path):
    config, values = configuration(tmp_path)
    operators = Operators(config, values)
    principal = operators.authenticate("Bearer " + OPERATOR_TOKEN)
    actor = principal.actor("project")
    assert (actor.provider, actor.connection_id, actor.native_id, actor.login, actor.is_bot) == (
        "gitlab",
        "ce-1",
        "42",
        "reviewer",
        False,
    )
    with pytest.raises(NotAuthorized):
        principal.actor("other")
    with pytest.raises(NotAuthorized):
        operators.authenticate("Bearer unknown")


def test_invalid_toml_configuration_error_cannot_echo_unknown_plaintext_secret(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text('private_key="do-not-echo-invalid-secret"\n')
    with pytest.raises(ValueError) as error:
        load(path)
    assert "do-not-echo" not in str(error.value)


def test_new_application_imports_no_retired_settings_models_or_engine():
    code = "import sys; import forge.runtime.app; retired=[name for name in sys.modules if name in ('forge.config','forge.main','forge.database','forge.models') or name.startswith(('forge.adaptive.','forge.durable.','forge.runs.'))]; assert not retired,retired"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
