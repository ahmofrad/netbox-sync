"""Configuration: .env loading, credentials, scan ranges, logging, validation.

Everything in this module is read from the environment at import time, right
after load_dotenv() runs, so importing any netbox_sync module picks up the
user's .env exactly like the old monolith did.
"""
import ipaddress
import os
from datetime import datetime

import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()
# Cron-safe fallback: dotenv's upward search normally finds the repo .env
# from any cwd, but make it explicit if NETBOX_URL is still missing.
if not os.getenv("NETBOX_URL"):
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

# ── credentials ──────────────────────────────────────────────────────────────
NETBOX_URL   = os.getenv("NETBOX_URL")
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")
REDFISH_USER = os.getenv("REDFISH_USER")
REDFISH_PASS = os.getenv("REDFISH_PASS")

STORAGE_USER = os.getenv("STORAGE_USER")
STORAGE_PASS = os.getenv("STORAGE_PASS")

SWITCH_USER = os.getenv("SWITCH_USER")
SWITCH_PASS = os.getenv("SWITCH_PASS")

CISCO_USER = os.getenv("CISCO_USER")
CISCO_PASS = os.getenv("CISCO_PASS")

FORTIGATE_USER = os.getenv("FORTIGATE_USER")
FORTIGATE_PASS = os.getenv("FORTIGATE_PASS")

RUCKUS_USER = os.getenv("RUCKUS_USER")
RUCKUS_PASS = os.getenv("RUCKUS_PASS")

HIKVISION_USER = os.getenv("HIKVISION_USER")
HIKVISION_PASS = os.getenv("HIKVISION_PASS")

REQUIRED_ENV_VARS = ("NETBOX_URL", "NETBOX_TOKEN",
                     "REDFISH_USER", "REDFISH_PASS",
                     "STORAGE_USER", "STORAGE_PASS",
                     "SWITCH_USER", "SWITCH_PASS")

def _validate_config():
    """Fail fast at startup if required .env variables are missing.
    Kept out of module scope so the modules stay importable for tests."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    # Cisco family is opt-in; its creds are required only when ranges are set.
    if os.getenv("CISCO_RANGES") and (not os.getenv("CISCO_USER")
                                      or not os.getenv("CISCO_PASS")):
        missing.append("CISCO_USER/CISCO_PASS (required when CISCO_RANGES is set)")
    # FortiGate family is opt-in: SSH creds + a non-empty token file.
    if os.getenv("FORTIGATE_RANGES"):
        if not os.getenv("FORTIGATE_USER") or not os.getenv("FORTIGATE_PASS"):
            missing.append("FORTIGATE_USER/FORTIGATE_PASS (required when FORTIGATE_RANGES is set)")
        token_path = os.getenv("FORTIGATE_TOKEN_FILE", FORTIGATE_TOKEN_FILE)
        if not _load_fortigate_tokens(token_path):
            missing.append(f"FortiGate token file missing or empty ({token_path})")
    # Ruckus family is opt-in; SSH creds required only when ranges are set.
    if os.getenv("RUCKUS_RANGES") and (not os.getenv("RUCKUS_USER")
                                       or not os.getenv("RUCKUS_PASS")):
        missing.append("RUCKUS_USER/RUCKUS_PASS (required when RUCKUS_RANGES is set)")
    # Hikvision family is opt-in; digest creds required only when ranges are set.
    if os.getenv("HIKVISION_RANGES") and (not os.getenv("HIKVISION_USER")
                                          or not os.getenv("HIKVISION_PASS")):
        missing.append("HIKVISION_USER/HIKVISION_PASS (required when HIKVISION_RANGES is set)")
    if missing:
        raise RuntimeError(f"Missing required .env variables: {', '.join(missing)}")

def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

# ── config – ranges ──────────────────────────────────────────────────────────
def _parse_ranges(env_name, default):
    """Range semantics: unset env var -> default; set-but-empty -> family
    disabled ([]); set -> comma-separated CIDR list."""
    val = os.getenv(env_name)
    if val is None:
        return list(default)
    return [r.strip() for r in val.split(",") if r.strip()]

DEFAULT_BMC_RANGES = [
    "192.0.2.0/27",
    "198.51.100.0/27",
]
BMC_RANGES = _parse_ranges("BMC_RANGES", DEFAULT_BMC_RANGES)

DEFAULT_STORAGE_RANGES = [
    "192.0.2.16/32",
    "198.51.100.16/32",
]
STORAGE_RANGES = _parse_ranges("STORAGE_RANGES", DEFAULT_STORAGE_RANGES)

DEFAULT_SAN_RANGES = [
    "192.0.2.32/29",
    "198.51.100.32/29",
]
SAN_RANGES = _parse_ranges("SAN_RANGES", DEFAULT_SAN_RANGES)

# Cisco family is opt-in: empty default means "disabled".
CISCO_RANGES = _parse_ranges("CISCO_RANGES", [])

# FortiGate family is opt-in: empty default means "disabled".
FORTIGATE_RANGES = _parse_ranges("FORTIGATE_RANGES", [])

# Ruckus family is opt-in: empty default means "disabled".
RUCKUS_RANGES = _parse_ranges("RUCKUS_RANGES", [])
RUCKUS_HA_MAP = os.getenv("RUCKUS_HA_MAP", "")

# Hikvision family is opt-in: empty default means "disabled".
HIKVISION_RANGES = _parse_ranges("HIKVISION_RANGES", [])

FORTIGATE_PORT     = int(os.getenv("FORTIGATE_PORT", "443"))
FORTIGATE_SSH_PORT = int(os.getenv("FORTIGATE_SSH_PORT", "22"))
FORTIGATE_ROLE     = os.getenv("DEFAULT_FORTIGATE_ROLE", "Firewall")
RUCKUS_PORT        = int(os.getenv("RUCKUS_PORT", "22"))
RUCKUS_ROLE        = os.getenv("DEFAULT_RUCKUS_ROLE", "Wireless Controller")
AP_ROLE            = os.getenv("DEFAULT_AP_ROLE", "Access Point")
HIKVISION_PORT     = int(os.getenv("HIKVISION_PORT", "80"))
HIKVISION_ROLE     = os.getenv("DEFAULT_HIKVISION_ROLE", "NVR")
HIKVISION_CAMERA_ROLE = os.getenv("DEFAULT_HIKVISION_CAMERA_ROLE", "Camera")
FORTIGATE_TOKEN_FILE = os.getenv(
    "FORTIGATE_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fortigate_tokens.txt"))

def _load_fortigate_tokens(path):
    """Load per-device FortiGate API tokens: "<ip[:port]> <token>" per line.
    '#' comments and blank lines allowed; port defaults to 443."""
    tokens = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        if os.getenv("FORTIGATE_RANGES"):
            log("WARN", f"FortiGate token file not found: {path}")
        return tokens
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            log("WARN", f"FortiGate token file: bad line {s!r} — skipped")
            continue
        host, token = parts[0], parts[1]
        if ":" in host:
            ip, port_s = host.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                log("WARN", f"FortiGate token file: bad port in {s!r} — skipped")
                continue
        else:
            ip, port = host, 443
        tokens[ip] = (port, token)
    return tokens

FORTIGATE_TOKENS = _load_fortigate_tokens(FORTIGATE_TOKEN_FILE)

REDFISH_PORT  = int(os.getenv("REDFISH_PORT", "443"))
STORAGE_PORT  = int(os.getenv("STORAGE_PORT", "443"))
SWITCH_PORT   = int(os.getenv("SWITCH_PORT", "22"))
STORAGE_AUTH_HASH = os.getenv("STORAGE_AUTH_HASH", "sha256").lower()
SCAN_WORKERS  = int(os.getenv("SCAN_WORKERS", "20"))
SERVER_ROLE   = os.getenv("DEFAULT_ROLE_NAME", "Server")
STORAGE_ROLE  = os.getenv("DEFAULT_STORAGE_ROLE", "Storage")
SWITCH_ROLE   = os.getenv("DEFAULT_SWITCH_ROLE", "SAN Switch")
CISCO_PORT    = int(os.getenv("CISCO_PORT", "22"))
CISCO_ROLE    = os.getenv("DEFAULT_CISCO_ROLE", "Switch")
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

# ── site assignment by IP range ──────────────────────────────────────────────
def _parse_site_ip_map(env_value):
    """Parse "cidr:Site,cidr:Site2" into [(IPv4Network, site)] sorted by
    prefix length descending (most specific first; stable on ties).
    Malformed entries are skipped with a WARN."""
    pairs = [p.strip() for p in (env_value or "").split(",") if p.strip()]
    out = []
    for pair in pairs:
        if ":" not in pair:
            log("WARN", f"SITE_IP_MAP entry {pair!r} is not 'cidr:Site' — skipped")
            continue
        cidr, site = pair.split(":", 1)
        try:
            out.append((ipaddress.ip_network(cidr.strip(), strict=False),
                        site.strip()))
        except ValueError as exc:
            log("WARN", f"SITE_IP_MAP entry {pair!r} has invalid CIDR ({exc}) — skipped")
    out.sort(key=lambda t: -t[0].prefixlen)
    return out

SITE_IP_MAP = _parse_site_ip_map(os.getenv("SITE_IP_MAP"))
