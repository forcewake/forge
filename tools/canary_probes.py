"""Synthetic TLS peer and installed-image CLI assertions sent on stdin; no checkout mount."""

CERTIFICATE = r"""
import datetime, ipaddress, json, os, pathlib, shutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import certifi, forge
assert os.getuid() != 0 and 'site-packages' in forge.__file__
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Owned synthetic canary')])
now = datetime.datetime.now(datetime.timezone.utc)
cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),True).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256())
root=pathlib.Path('/data'); shutil.copytree(pathlib.Path(certifi.where()).parent,root,dirs_exist_ok=True)
ca=cert.public_bytes(serialization.Encoding.PEM); (root/'owned-ca.pem').write_bytes(ca)
(root/'owned-key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
(root/'cacert.pem').write_bytes(pathlib.Path(certifi.where()).read_bytes()+ca)
for filename in ('owned-ca.pem','owned-key.pem','cacert.pem'): (root/filename).chmod(0o644)
print(json.dumps({'trust':str(pathlib.Path(certifi.where()).parent)}))
"""

PROBE = r'''
import asyncio, hashlib, importlib.util, json, os, pathlib, ssl, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import forge
from forge.budget.strict import GUARD_DIGEST, ALLOWED_ROUTES, TrustedPolicy
assert os.getuid() != 0 and 'site-packages' in forge.__file__
assert all(importlib.util.find_spec('forge.'+name) is None for name in ('main','worker','runs','adaptive','durable','models','integrations'))
root = pathlib.Path('/tmp/canary'); root.mkdir()
policy = {'schema':'forge.strict-budget/1','litellm_version':'1.106.2','models':{'canary-model':{'native_model':'anthropic/synthetic','input_rate':'0.000002','output_rate':'0.000005','max_input_tokens':200000,'max_output_tokens':64000}}}
raw = json.dumps(policy).encode(); (root/'pricing.json').write_bytes(raw)
class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        from urllib.parse import urlsplit, parse_qs
        route = urlsplit(self.path)
        if route.path == '/forge/budget/readiness':
            value = {'schema':'forge.strict-budget-readiness/1','nonce':parse_qs(route.query)['nonce'][0],'guard_digest':GUARD_DIGEST,'policy_digest':TrustedPolicy.parse(raw).digest,'artifact_digest':'a'*64,'litellm_version':'1.106.2','models':['canary-model'],'routes':list(ALLOWED_ROUTES),'durable_unknown_holds':True}
        elif route.path == '/api/v4/projects/123':
            value = {'id':123,'path_with_namespace':'owner/project','default_branch':'main','archived':False}
        elif route.path == '/api/v4/projects/123/repository/branches/main':
            value = {'name':'main','protected':True,'commit':{'id':'a'*40}}
        else:
            self.send_error(503); return
        body=json.dumps(value).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
server = ThreadingHTTPServer(('127.0.0.1',0),Peer)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain('/canary/owned-ca.pem','/canary/owned-key.pem')
server.socket=context.wrap_socket(server.socket,server_side=True); threading.Thread(target=server.serve_forever,daemon=True).start()
peer='https://127.0.0.1:'+str(server.server_port)
config = f"""schema = "forge.runtime/1"
database_url_env = "CANARY_DATABASE"
secret_key_env = "CANARY_SESSION"
public_url = "https://forge.invalid"
blob_directory = "/tmp/canary/blobs"
lane_requirements_url = "https://release.invalid/lane.txt"
lane_requirements_sha256 = "{'1'*64}"
[gateway]
url = "{peer}"
admin_key_env = "CANARY_ADMIN"
policy_path = "/tmp/canary/pricing.json"
artifact_digest = "{'a'*64}"
[[connections]]
key = "canary"
provider = "gitlab"
api_url = "{peer}"
git_url = "{peer}"
oidc_issuer = "{peer}"
oidc_jwks_url = "{peer}/keys"
webhook_secret_env = "CANARY_WEBHOOK"
tls_ca_path = "/canary/owned-ca.pem"
[[connections.repositories]]
key = "project"
native_id = "123"
full_name = "owner/project"
bot_id = "99"
bot_login = "project_123_bot_canary"
approver_ids = ["42"]
read_token_env = "CANARY_READ"
write_token_env = "CANARY_WRITE"
[connections.repositories.policy]
driver = "claude_code"
model = "canary-model"
target_branch = "main"
verification = {{ required_checks = ["unit"] }}
write_policy = {{ name = "no_dependencies", denied_paths = [".forge.yml", ".gitlab-ci.yml"], denied_prefixes = [".github/"] }}
limits = {{ usd = "10", calls = 10, tokens = 200000, wallclock_seconds = 600 }}
planning_allowance = {{ usd = "1", calls = 2, tokens = 40000 }}
closing_reserve = {{ usd = "2", calls = 2, tokens = 60000 }}
[[operators]]
key = "canary"
token_env = "CANARY_OPERATOR"
grants = [{{ repository = "project", native_id = "42", login = "reviewer" }}]"""
(root/'forge.toml').write_text(config)
os.environ.update(CANARY_SESSION='synthetic-session-at-least32bytes',CANARY_ADMIN='synthetic-admin',CANARY_WEBHOOK='synthetic-webhook-at-least32bytes',CANARY_READ='synthetic-read',CANARY_WRITE='synthetic-write',CANARY_OPERATOR='synthetic-operator-at-least32bytes')
def cli(*args, expected=0):
    value=subprocess.run(args,capture_output=True,text=True,timeout=30)
    assert value.returncode==expected,(args,value.returncode,value.stdout,value.stderr)
    return value.stdout
async def sql(query, url=None):
    engine=create_async_engine(url or os.environ['CANARY_DATABASE'])
    try:
        async with engine.begin() as connection:
            result=await connection.execute(text(query)); return result.fetchall() if result.returns_rows else []
    finally: await engine.dispose()
def rows(): return asyncio.run(sql("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"))
def check_schema(expected=0):
    os.environ['DATABASE_URL']=os.environ['CANARY_DATABASE']
    cli('forge-schema','check',expected=expected)
assert rows()==[]
check_schema(expected=1)
cli('forge','--config',str(root/'forge.toml'),'serve',expected=3)
assert rows()==[], 'startup must never initialize an empty database'
cli('forge','--config',str(root/'forge.toml'),'install')
check_schema()
cli('forge','--config',str(root/'forge.toml'),'install',expected=1)
installed=rows(); assert ('schema_installation',) in installed and ('runs',) in installed
fresh=os.environ['CANARY_DATABASE']; os.environ['CANARY_DATABASE']=os.environ['CANARY_LEGACY_DATABASE']
for query in ("CREATE TABLE alembic_version (version_num TEXT NOT NULL)","INSERT INTO alembic_version VALUES ('032')","CREATE TABLE historical_evidence (body TEXT NOT NULL)","INSERT INTO historical_evidence VALUES ('immutable-old-evidence')"): asyncio.run(sql(query))
before=rows()
check_schema(expected=1)
cli('forge','--config',str(root/'forge.toml'),'serve',expected=3)
cli('forge','--config',str(root/'forge.toml'),'install',expected=1)
assert rows()==before and asyncio.run(sql('SELECT body FROM historical_evidence'))==[('immutable-old-evidence',)]
assert asyncio.run(sql('SELECT version_num FROM alembic_version'))==[('032',)]
os.environ['CANARY_DATABASE']=fresh
check_schema()
app=subprocess.Popen(['forge','--config',str(root/'forge.toml'),'serve','--port','8420'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    for attempt in range(100):
        assert app.poll() is None, 'runtime exited before health'
        try:
            with urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=2) as response: health=json.load(response)
            break
        except OSError: time.sleep(.1)
    else: raise AssertionError('runtime health timeout')
    assert health=={'service':'forge','version':forge.__version__}
    with urllib.request.urlopen('http://127.0.0.1:8420/readyz',timeout=10) as response: assert json.load(response)=={'schema':'checked','native_budget':'verified'}
    try: urllib.request.urlopen('http://127.0.0.1:8420/api/v1/runs',timeout=5)
    except urllib.error.HTTPError as error: assert error.code==401
    else: raise AssertionError('operator API accepted unauthenticated request')
    report=json.loads(cli('forge','--config',str(root/'forge.toml'),'doctor')); assert report['status']=='ok' and all(row['status']=='pass' for row in report['checks'])
finally:
    app.terminate(); app.wait(timeout=10); server.shutdown()
print(json.dumps({'version':forge.__version__,'installed_outside_checkout':True,'nonroot':True,'explicit_init_and_check':True,'startup_zero_ddl':True,'old_schema_refused_data_preserved':True,'health_and_readiness':True,'native_peers':'synthetic TLS guard and GitLab read fixture; no hosted/model calls'}))
'''
