"""Configuration: .env loading, credentials, scan ranges, logging, validation.

Everything in this module is read from the environment at import time, right
after load_dotenv() runs, so importing any netbox_sync module picks up the
user's .env exactly like the old monolith did.
"""
import os
from datetime import datetime

import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# ── credentials ──────────────────────────────────────────────────────────────
NETBOX_URL   = os.getenv("NETBOX_URL")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")
REDFISH_USER = os.getenv("REDFISH_USER")
REDFISH_PASS = os.getenv("REDFISH_PASS")

STORAGE_USER = os.getenv("STORAGE_USER")
STORAGE_PASS = os.getenv("STORAGE_PASS")

SWITCH_USER = os.getenv("SWITCH_USER")
SWITCH_PASS = os.getenv("SWITCH_PASS")

REQUIRED_ENV_VARS = ("NETBOX_URL", "NETBOX_TOKEN",
                     "REDFISH_USER", "REDFISH_PASS",
                     "STORAGE_USER", "STORAGE_PASS",
                     "SWITCH_USER", "SWITCH_PASS")

def _validate_config():
    """Fail fast at startup if required .env variables are missing.
    Kept out of module scope so the modules stay importable for tests."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"Missing required .env variables: {', '.join(missing)}")

def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

# ── config – ranges ──────────────────────────────────────────────────────────
DEFAULT_BMC_RANGES = [
    "192.0.2.0/27",
    "198.51.100.0/27",
]
BMC_RANGES = DEFAULT_BMC_RANGES
if os.getenv("BMC_RANGES"):
    BMC_RANGES = [r.strip() for r in os.getenv("BMC_RANGES").split(",") if r.strip()]

DEFAULT_STORAGE_RANGES = [
    "192.0.2.16/32",
    "198.51.100.16/32",
]
STORAGE_RANGES = DEFAULT_STORAGE_RANGES
if os.getenv("STORAGE_RANGES"):
    STORAGE_RANGES = [r.strip() for r in os.getenv("STORAGE_RANGES").split(",") if r.strip()]

DEFAULT_SAN_RANGES = [
    "192.0.2.32/29",
    "198.51.100.32/29",
]
SAN_RANGES = DEFAULT_SAN_RANGES
if os.getenv("SAN_RANGES"):
    SAN_RANGES = [r.strip() for r in os.getenv("SAN_RANGES").split(",") if r.strip()]

REDFISH_PORT  = int(os.getenv("REDFISH_PORT", "443"))
STORAGE_PORT  = int(os.getenv("STORAGE_PORT", "443"))
SWITCH_PORT   = int(os.getenv("SWITCH_PORT", "22"))
STORAGE_AUTH_HASH = os.getenv("STORAGE_AUTH_HASH", "sha256").lower()
SCAN_WORKERS  = int(os.getenv("SCAN_WORKERS", "20"))
SERVER_ROLE   = os.getenv("DEFAULT_ROLE_NAME", "Server")
STORAGE_ROLE  = os.getenv("DEFAULT_STORAGE_ROLE", "Storage")
SWITCH_ROLE   = os.getenv("DEFAULT_SWITCH_ROLE", "SAN Switch")
DEFAULT_MFR   = "HPE"
DEFAULT_SITE  = os.getenv("DEFAULT_SITE_NAME", "")
OFFLINE_THRESHOLD = int(os.getenv("OFFLINE_THRESHOLD", "2"))

# ── site keyword mapping ─────────────────────────────────────────────────────
# Example mapping — replace with your own site keywords, or set SITE_KEYWORD_MAP
# in your .env as "keyword1:Site1,keyword2:Site2"
SITE_KEYWORD_MAP = [
    ("site1", "Site1"),
    ("site2", "Site2"),
]
if os.getenv("SITE_KEYWORD_MAP"):
    SITE_KEYWORD_MAP = [
        tuple(pair.split(":", 1))
        for pair in os.getenv("SITE_KEYWORD_MAP").split(",")
        if ":" in pair
    ]
SITE_UNKNOWN = DEFAULT_SITE or "Unknown"

# ── logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

def log(level, msg):
    if _LOG_LEVELS.get(level, 20) < _LOG_LEVELS.get(LOG_LEVEL, 20):
        return
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}")
