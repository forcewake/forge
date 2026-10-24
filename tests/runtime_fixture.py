"""Owned TLS native API and cold installed SQLite; no provider credentials/network."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import ssl
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx

from forge.runtime.settings import RuntimeSettings
from tests import gitlab_engine_fixture as native
from tests.test_domain_approved_input import spec

OPERATOR_TOKEN = "synthetic-operator-opaque-token-at-least32bytes"
WEBHOOK_SECRET = "synthetic-webhook-secret-at-least-32-bytes"


def configuration(root: Path, *, native_url="https://ce.example", ca=None):
    from forge.domain.budget import BudgetLimits, Resources
    from decimal import Decimal

    policy = spec(
        limits=BudgetLimits(usd=Decimal("10"), calls=20, tokens=200000, wallclock_seconds=600),
        closing_reserve=Resources(usd=Decimal("2"), calls=2, tokens=60000),
        planning_allowance=Resources(usd=Decimal("1"), calls=3, tokens=40000),
    ).model_dump(mode="json")
    from forge.domain.run_spec import RunPolicy

    policy = {key: policy[key] for key in RunPolicy.model_fields}
    policy_path = root / "pricing.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema": "forge.strict-budget/1",
                "litellm_version": "1.106.2",
                "models": {
                    "test-model": {
                        "native_model": "anthropic/approved",
                        "input_rate": "0.000002",
                        "output_rate": "0.000005",
                        "max_input_tokens": 200000,
                        "max_output_tokens": 64000,
                    }
                },
            }
        )
    )
    document = {
        "schema": "forge.runtime/1",
        "database_url_env": "SYNTHETIC_DATABASE_URL",
        "secret_key_env": "SYNTHETIC_SECRET",
        "public_url": "https://forge.invalid",
        "blob_directory": str(root / "blobs"),
        "lane_requirements_url": "https://release.invalid/lane.txt",
        "lane_requirements_sha256": "1" * 64,
        "gateway": {
            "url": "https://gateway.invalid",
            "admin_key_env": "SYNTHETIC_ADMIN",
            "policy_path": str(policy_path),
            "artifact_digest": "a" * 64,
            "settle_seconds": 0,
        },
        "connections": [
            {
                "key": "ce-1",
                "provider": "gitlab",
                "api_url": native_url,
                "git_url": native_url,
                "oidc_issuer": native_url,
                "oidc_jwks_url": native_url + "/keys",
                "webhook_secret_env": "SYNTHETIC_WEBHOOK",
                "tls_ca_path": str(ca) if ca else None,
                "repositories": [
                    {
                        "key": "project",
                        "native_id": "123",
                        "full_name": "new/repository",
                        "bot_id": "99",
                        "bot_login": "project_123_bot_forge",
                        "approver_ids": ["42"],
                        "read_token_env": "SYNTHETIC_READ",
                        "write_token_env": "SYNTHETIC_WRITE",
                        "policy": policy,
                    }
                ],
            }
        ],
        "operators": [
            {
                "key": "owner",
                "token_env": "SYNTHETIC_OPERATOR",
                "grants": [{"repository": "project", "native_id": "42", "login": "reviewer"}],
            }
        ],
    }
    config = RuntimeSettings.model_validate_json(json.dumps(document))
    values = {
        "SYNTHETIC_DATABASE_URL": "sqlite+aiosqlite:///" + str(root / "runtime.db"),
        "SYNTHETIC_SECRET": "synthetic-runtime-signing-at-least32bytes",
        "SYNTHETIC_ADMIN": "synthetic-admin",
        "SYNTHETIC_WEBHOOK": WEBHOOK_SECRET,
        "SYNTHETIC_READ": "synthetic-read",
        "SYNTHETIC_WRITE": "synthetic-write",
        "SYNTHETIC_OPERATOR": OPERATOR_TOKEN,
    }
    return config, values


def certificate(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Owned Forge synthetic native TLS")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    private, public = root / "native-key.pem", root / "native-cert.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    public.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return private, public


@contextmanager
def native_server(root):
    wire = native.Wire()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            count = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(count)
            request = httpx.Request(
                self.command,
                "https://ce.example" + self.path,
                headers=dict(self.headers),
                content=body,
            )
            response = wire(request)
            payload = response.content
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", response.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    private, public = certificate(root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(public, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield wire, "https://127.0.0.1:" + str(server.server_port), public
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
