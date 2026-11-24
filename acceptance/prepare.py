"""Generate synthetic, local-only acceptance environment files."""
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
RESULTS = ROOT / "results"
RUNTIME.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

previous = {}
env_path = RUNTIME / "netbox.env"
if env_path.exists():
    previous = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines()
                    if "=" in line)
credentials_path = RUNTIME / "credentials.json"
credentials = json.loads(credentials_path.read_text(encoding="utf-8")) if credentials_path.exists() else {}
secret_names = ("DB_PASSWORD", "NETBOX_TOKEN", "MCP_NETBOX_TOKEN", "REDFISH_PASS",
                "STORAGE_PASS", "SWITCH_PASS", "SECRET_KEY", "API_TOKEN_PEPPER_1")
values = {name: previous.get(name) or secrets.token_urlsafe(32) for name in secret_names}

(RUNTIME / "postgres.env").write_text(
    "POSTGRES_DB=netbox_acceptance\nPOSTGRES_USER=netbox_acceptance\n"
    f"POSTGRES_PASSWORD={values['DB_PASSWORD']}\n", encoding="utf-8")
(RUNTIME / "netbox.env").write_text(
    "DB_NAME=netbox_acceptance\nDB_USER=netbox_acceptance\n"
    f"DB_PASSWORD={values['DB_PASSWORD']}\n"
    f"SECRET_KEY={values['SECRET_KEY']}\n"
    f"API_TOKEN_PEPPER_1={values['API_TOKEN_PEPPER_1']}\n"
    f"NETBOX_URL=http://netbox:8080\nNETBOX_TOKEN={values['NETBOX_TOKEN']}\n"
    f"MCP_NETBOX_TOKEN={values['MCP_NETBOX_TOKEN']}\n"
    "NETBOX_VERIFY_TLS=false\n"
    f"REDFISH_USER=fixture-user\nREDFISH_PASS={values['REDFISH_PASS']}\n"
    f"STORAGE_USER=fixture-user\nSTORAGE_PASS={values['STORAGE_PASS']}\n"
    f"SWITCH_USER=fixture-user\nSWITCH_PASS={values['SWITCH_PASS']}\n"
    "BMC_RANGES=\nSTORAGE_RANGES=\nSAN_RANGES=\n"
    "NETBOX_SYNC_ACCEPTANCE_FIXTURES=1\nPYTHONPATH=/opt/netbox-sync-acceptance\n"
    "NETBOX_SYNC_LOCK_FILE=/tmp/netbox-sync-acceptance.lock\n"
    "NETBOX_SYNC_ACCEPTANCE_DELAY=2\n", encoding="utf-8")
credentials_path.write_text(json.dumps({
    "username": "acceptance-admin",
    "password": credentials.get("password") or secrets.token_urlsafe(32),
}) + "\n", encoding="utf-8")
print("Prepared synthetic acceptance files under acceptance/runtime/")
