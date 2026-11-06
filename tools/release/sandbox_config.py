"""Prepare one private, explicitly scoped 1.0 release sandbox without services."""

import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

SCHEMA = "forge.release-sandbox/1"
RESERVED = frozenset(
    (
        "PATH",
        "HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "FORGE_RELEASE_SCOPE",
        "FORGE_RELEASE_SANDBOX",
        "FORGE_RELEASE_SMOKE_ACTOR_TOKEN",
    )
)


def pinned_image(value: str) -> str:
    if re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("release image requires an immutable registry digest")
    return value


def scope_name(value: str) -> str:
    if re.fullmatch(r"forge-release-[a-z0-9-]{1,90}", value) is None:
        raise ValueError("release sandbox scope is invalid")
    return value


def strings(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("private values must be an object of strings")
    return value


def private_write(path: Path, body: str) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(body)


def pin_requirements(raw: str, manifest: dict) -> str:
    top, separator, rest = raw.partition("\n[")
    for field in ("lane_requirements_url", "lane_requirements_sha256"):
        pattern = re.compile(r"^" + field + r"\s*=.*$", re.MULTILINE)
        if len(pattern.findall(top)) != 1:
            raise ValueError("runtime requires an explicit top-level " + field)
        top = pattern.sub(field + " = " + json.dumps(manifest[field]), top)
    return top + separator + rest


def prepare(
    bundle: Path, manifest_path: Path, output: Path, provider: str, repository: str, scope: str
) -> Path:
    scope_name(scope)
    source = json.loads(bundle.read_text())
    if (
        set(source) != {"schema", "runtime_toml", "policy_json", "certificates", "environment"}
        or source["schema"] != SCHEMA
    ):
        raise ValueError("release sandbox requires its closed private bundle")
    manifest = json.loads(manifest_path.read_text())
    pinned_image(manifest["image"])
    raw = pin_requirements(source["runtime_toml"], manifest)
    config = tomllib.loads(raw)
    (connection,) = config["connections"]
    (repo,) = connection["repositories"]
    if connection["provider"] != provider or repo["key"] != repository or repo.get("neighbors"):
        raise ValueError("release sandbox must contain only the selected native repository")
    if (
        config["blob_directory"] != "/data/blobs"
        or config["gateway"]["policy_path"] != "/etc/forge/policy.json"
    ):
        raise ValueError("release config must use its private policy and blob mounts")
    if urlsplit(config["gateway"]["url"]).scheme != "https":
        raise ValueError("release requires the real guarded HTTPS gateway")
    if repo["policy"]["write_policy"].get("allowed_globs") != ["forge_release_smoke.py"]:
        raise ValueError("release write authority must be restricted to its implementation file")
    if "release-smoke-oracle" not in repo["policy"]["verification"][
        "required_checks"
    ] or "release-smoke-oracle" not in repo.get("verification_failure_jobs", []):
        raise ValueError("release requires the independent oracle and native failure proof")
    environment = strings(source["environment"])
    if set(environment) & RESERVED or any(
        re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) is None for key in environment
    ):
        raise ValueError("private runtime environment cannot replace process authority")
    declared = {
        config["secret_key_env"],
        config["gateway"]["admin_key_env"],
        connection["webhook_secret_env"],
    }
    declared.update(operator["token_env"] for operator in config["operators"])
    declared.update(connection[k] for k in ("app_private_key_env",) if k in connection)
    declared.update(repo[k] for k in ("read_token_env", "write_token_env") if repo.get(k))
    if set(environment) != declared:
        raise ValueError("private environment must match exactly the declared runtime secret names")
    database_env = config["database_url_env"]
    if (
        database_env in RESERVED | declared
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", database_env) is None
    ):
        raise ValueError("database environment cannot replace runtime or process authority")
    environment[database_env] = f"postgresql+asyncpg://forge:forge@{scope}-pg:5432/forge"
    certificates = strings(source["certificates"])
    if any(re.fullmatch(r"[A-Za-z0-9_-]+\.pem", name) is None for name in certificates):
        raise ValueError("certificate paths must be confined private PEM files")
    if connection.get("tls_ca_path") and connection["tls_ca_path"] not in {
        "/etc/forge/" + name for name in certificates
    }:
        raise ValueError("configured native CA is missing from the private mount")
    json.loads(source["policy_json"])
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    private_write(output / "forge.toml", raw)
    private_write(output / "policy.json", source["policy_json"])
    for name, body in certificates.items():
        private_write(output / name, body)
    (output / "blobs").mkdir(mode=0o700)
    control = {
        "schema": SCHEMA,
        "provider": provider,
        "repository": repository,
        "scope": scope,
        "image": manifest["image"],
        "config_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "environment": environment,
    }
    private_write(output / "control.json", json.dumps(control))
    return output / "control.json"
