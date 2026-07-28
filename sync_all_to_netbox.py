#!/usr/bin/env python3
"""
sync_all_to_netbox.py
Merged automation: scans IP ranges for iLO/Redfish BMCs (servers),
HPE storage arrays, and HPE B-Series (Brocade OEM) SAN switches,
auto-creates/updates devices, interfaces and inventory in NetBox,
and marks unreachable devices offline. Runs at 00:00 and 12:00 daily.
"""
import hashlib
import os
import re
import time
import socket
import ipaddress
import urllib3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET

import requests
import pynetbox
import schedule
import paramiko
from dotenv import load_dotenv

from models import SERVER_MODEL_MAP, STORAGE_MODEL_MAP, SWITCH_MODEL_MAP

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# credentials
# ═══════════════════════════════════════════════════════════════════════════════
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
    Kept out of module scope so the module stays importable for tests."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"Missing required .env variables: {', '.join(missing)}")

nb = None

def _env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def get_netbox():
    global nb
    if nb is not None:
        return nb
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    # TLS verification for NetBox is opt-in (NETBOX_VERIFY_TLS=true) since
    # many internal NetBox installs use self-signed certs.
    nb.http_session.verify = _env_bool("NETBOX_VERIFY_TLS", False)
    return nb

# ═══════════════════════════════════════════════════════════════════════════════
# config – ranges
# ═══════════════════════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════════════════════
# inventory item roles — resolved by NAME via get_or_create_inventory_role().
# Role IDs are DB-sequence-dependent and NOT portable between NetBox
# instances, so nothing here may hardcode them.
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# site keyword mapping
# ═══════════════════════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════════════════════
# model name normalization — see models.py (SERVER_MODEL_MAP / STORAGE_MODEL_MAP)
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════════
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

def log(level, msg):
    if _LOG_LEVELS.get(level, 20) < _LOG_LEVELS.get(LOG_LEVEL, 20):
        return
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}")

def slugify(s):
    return re.sub(r'[^a-z0-9-]', '-', s.lower().strip())[:50].strip('-')

def normalize_model(model, model_map):
    if not model: return None
    return model_map.get(model.strip().lower(), model.strip())

def resolve_site_from_name(server_name):
    name_lower = (server_name or "").lower()
    for keyword, site in SITE_KEYWORD_MAP:
        if keyword in name_lower:
            return site
    return SITE_UNKNOWN

def gib_from_bytes(v):
    try: return int(round(int(v) / (1024**3)))
    except Exception: return None

def _to_int(v):
    try: return int(v)
    except Exception:
        try: return int(float(v))
        except Exception: return None

def _capacity_to_bytes(obj):
    if not isinstance(obj, dict): return None
    if obj.get("CapacityBytes") is not None:
        try: return int(obj["CapacityBytes"])
        except Exception: pass
    for k, mult in [("CapacityGiB", 1024**3), ("CapacityMiB", 1024**2),
                    ("CapacityGB",  1000**3), ("CapacityMB",  1000**2)]:
        if obj.get(k) is not None:
            try: return int(float(obj[k]) * mult)
            except Exception: pass
    return None

def _pick(d, keys):
    if not isinstance(d, dict): return None
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip(): return v
    return None

def _invalid_serial(serial):
    s = str(serial or "").strip()
    return not s or s.upper() in ("N/A", "NOT AVAILABLE", "UNKNOWN", "NONE", "0", "")

def _add_inventory_item(inventory, name, manufacturer, part_number, serial, description, role_id=None):
    serial = str(serial or "").strip()
    if _invalid_serial(serial): return
    if serial in inventory: return
    inventory[serial] = {
        "name":         str(name or "Unknown").strip()[:64],
        "manufacturer": str(manufacturer or "").strip() or None,
        "part_number":  str(part_number or "").strip() or None,
        "serial":       serial,
        "description":  str(description or "").strip()[:200],
        "role":         role_id,
    }

def _make_add_item(inventory):
    def add_item(name, manufacturer, part_number, serial, description, role_id=None):
        _add_inventory_item(inventory, name, manufacturer, part_number, serial, description, role_id)
    return add_item

def _get_location(obj):
    if not isinstance(obj, dict): return None
    pl = obj.get("PhysicalLocation") or {}
    sl = (pl.get("PartLocation") or {}).get("ServiceLabel") if isinstance(pl, dict) else None
    if sl: return sl
    loc = obj.get("Location")
    if isinstance(loc, str): return loc
    if isinstance(loc, dict): return loc.get("Info") or loc.get("ServiceLabel")
    for k in ["Bay", "Slot", "Position", "Id", "Name"]:
        v = obj.get(k)
        if v:
            if isinstance(v, str): return v
            if isinstance(v, dict): return v.get("ServiceLabel") or v.get("Info")
    return None

def _get_oem(sys_data):
    if not isinstance(sys_data, dict): return {}
    oem = sys_data.get("Oem") or {}
    if isinstance(oem, dict): return oem.get("Hpe") or oem.get("Hp") or {}
    return {}

def _chassis_url(sys):
    cl = sys.get("Links", {}).get("Chassis") if isinstance(sys.get("Links"), dict) else None
    if isinstance(cl, list) and cl: return cl[0].get("@odata.id") if isinstance(cl[0], dict) else None
    if isinstance(cl, dict): return cl.get("@odata.id")
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# smart item naming (server)
# ═══════════════════════════════════════════════════════════════════════════════
_STANDARD_SIZES_GB = [
    1, 2, 4, 8, 16, 32, 64,
    120, 128, 160, 200, 240, 250, 300, 320, 400, 480, 500, 512,
    600, 800, 900, 960, 1000, 1200, 1600, 1800, 1920, 2000, 2400,
    3000, 3200, 3840, 4000, 6000, 7680, 8000, 10000, 12000,
    14000, 15000, 16000, 18000, 20000, 24000,
]

def _snap_to_standard(val_gb):
    for std in _STANDARD_SIZES_GB:
        if abs(val_gb - std) / std <= 0.05:
            return std
    return None

def _bytes_to_human(b):
    if not b: return None
    for unit, div in [("TB", 1e12), ("GB", 1e9), ("MB", 1e6)]:
        val = b / div
        if val >= 1:
            if unit == "GB":
                snapped = _snap_to_standard(val)
                if snapped: return f"{snapped}GB"
            if unit == "TB":
                val_gb = b / 1e9
                snapped = _snap_to_standard(val_gb)
                if snapped and snapped >= 1000:
                    tb = snapped / 1000
                    if tb == int(tb): return f"{int(tb)}TB"
                    s = f"{tb:.1f}".rstrip('0').rstrip('.')
                    return f"{s}TB"
            if val == int(val): return f"{int(val)}{unit}"
            s = f"{val:.1f}".rstrip('0').rstrip('.')
            return f"{s}{unit}"
    return f"{b}B"

def _mib_to_human(mib):
    if not mib: return None
    gib = mib / 1024
    if gib >= 1: return f"{int(round(gib))}GB"
    return f"{mib}MB"

def name_cpu(p):
    model = p.get("Model") or ""
    short = re.sub(r'^(Intel|AMD)\s+', '', model).strip()
    return short[:64] if short else "CPU"

def name_ram(mm):
    cap_mib  = _to_int(mm.get("CapacityMiB"))
    speed    = _to_int(mm.get("OperatingSpeedMhz") or mm.get("OperatingSpeedMHz"))
    mem_type = mm.get("MemoryDeviceType") or mm.get("MemoryType") or "RAM"
    mem_type = re.sub(r'\s+SDRAM.*', '', str(mem_type)).strip()
    cap_str  = _mib_to_human(cap_mib) if cap_mib else None
    if cap_str and speed:  return f"RAM {cap_str} {speed}"
    if cap_str:            return f"RAM {cap_str}"
    return "RAM"

def name_disk(drv):
    cap_b     = _capacity_to_bytes(drv)
    media     = (drv.get("MediaType") or "").upper()
    protocol  = (drv.get("Protocol") or ("SAS" if drv.get("CapableSpeedGbs") else "")).upper()
    cap_str   = _bytes_to_human(cap_b) if cap_b else None
    if not protocol:
        rpm = drv.get("RotationSpeedRPM")
        if media == "SSD": protocol = "SSD"
        elif rpm: protocol = "SAS" if int(rpm) > 0 else "SATA"
    prefix = "SSD" if media == "SSD" else "HDD"
    parts = [prefix]
    if cap_str: parts.append(cap_str)
    if protocol and protocol not in ("SSD",): parts.append(protocol)
    return " ".join(parts)

def name_psu(psu):
    watts = psu.get("PowerCapacityWatts")
    model = _pick(psu, ["Model", "Name"]) or ""
    if not watts:
        m = re.search(r'(\d{3,4})\s*W', model, re.IGNORECASE)
        if m: watts = m.group(1)
    if watts: return f"PSU {watts}W"
    return "PSU"

def name_nic(adapter_name, pci_info=None):
    aname = adapter_name or "NIC"
    short = re.search(r'(\d+\w*(?:SFP\+?|FLR|FLB|T\b|i\b))', aname)
    model_short = short.group(1) if short else None
    loc_short = ""
    if pci_info:
        loc = pci_info.get("DeviceLocation") or ""
        loc_short = (loc.replace("PCI-E Slot", "Slot")
                       .replace("Flexible LOM", "FlexLOM")
                       .replace("Embedded LOM", "EmbLOM")
                       .replace("Embedded", "Emb")
                       .replace(" ", "").strip())
    if model_short and loc_short: return f"{model_short}-{loc_short}"
    if model_short: return model_short
    if loc_short: return f"NIC-{loc_short}"
    return "NIC"

def name_hba(name_str, device_location):
    loc_short = (device_location or "")
    loc_short = (loc_short.replace("PCI-E Slot","Slot").replace(" ","").strip())
    short = re.search(r'(\d+\w*(?:Gb|GFC|HBA|FC))', name_str or "")
    model_short = short.group(1) if short else None
    if model_short and loc_short: return f"HBA-{model_short}-{loc_short}"
    if loc_short: return f"HBA-{loc_short}"
    return "HBA"

def is_ssd(drv):
    media = (drv.get("MediaType") or "").upper()
    if media == "SSD": return True
    if media == "HDD": return False
    model = (drv.get("Model") or "").upper()
    if any(k in model for k in ("SSD", "FLASH", "MLC", "TLC", "NVME", "SRI")):
        return True
    return bool(re.search(r'\b(?:EO|RI|WI)\b', model))

# ═══════════════════════════════════════════════════════════════════════════════
# smart item naming (storage)
# ═══════════════════════════════════════════════════════════════════════════════
def parse_storage_size_bytes(size_str, size_numeric=None):
    if size_str:
        m = re.match(r"([\d.]+)\s*(TB|GB|MB)", str(size_str).strip(), re.IGNORECASE)
        if m:
            val = float(m.group(1))
            mult = {"TB": 1024 ** 4, "GB": 1024 ** 3, "MB": 1024 ** 2}
            return int(val * mult[m.group(2).upper()])
    if size_numeric is not None:
        try:
            n = int(size_numeric)
            if n <= 0: return None
            return n * 1024 * 1024
        except Exception: pass
    return None

def is_ssd_storage(props):
    # Field names differ between "show disks" (drive-type, model)
    # and "show disk-parameters" (disk-type, disk-description)
    model = str(
        props.get("model") or
        props.get("disk-description") or
        props.get("description") or ""
    ).upper()
    dtype = str(
        props.get("drive-type") or     # show disks (newer firmware)
        props.get("disk-type") or      # show disk-parameters (older firmware)
        props.get("type") or ""
    ).upper()
    if "SSD" in dtype or "FLASH" in dtype: return True
    if "SSD" in model or "FLASH" in model: return True
    if "HDD" in dtype or "SAS" in dtype or "SATA" in dtype: return False
    return False

def name_storage_disk(props):
    media = "SSD" if is_ssd_storage(props) else "HDD"
    # Three possible field-name sets across MSA firmware versions:
    #   show disks           (MSA 2060): size, model
    #   show disk-parameters  (older):   total-size, disk-description
    #   show disk-statistics  (MSA 2040): no size/model, use location/durable-id
    size  = (props.get("size") or props.get("total-size")
             or props.get("formatted-size") or props.get("raw-size"))
    model = (props.get("model") or props.get("disk-description")
             or props.get("description") or props.get("vendor"))
    location = props.get("location") or props.get("durable-id") or ""
    parts = [media]
    if size:
        parts.append(str(size))
    elif model:
        parts.append(str(model)[:30])
    elif location:
        # disk-statistics rows: use location as the distinguishing suffix
        parts.append(str(location))
    return " ".join(parts)[:64]

def name_storage_psu(props):
    loc = props.get("location") or props.get("enclosure-id") or ""
    return f"PSU {loc}".strip()[:64]

def name_storage_controller(props):
    cid = props.get("controller-id") or props.get("durable-id") or "CTRL"
    return f"Controller {cid}"[:64]


# ═══════════════════════════════════════════════════════════════════════════════
# IP scanning
# ═══════════════════════════════════════════════════════════════════════════════
def expand_ranges(ranges):
    ips = []
    for cidr in ranges:
        net = ipaddress.ip_network(cidr, strict=False)
        if net.num_addresses == 1:
            ips.append(str(net.network_address))
        else:
            ips.extend(str(h) for h in net.hosts())
    return ips

def is_port_open(ip, port, timeout=5, retries=3, retry_delay=2):
    for attempt in range(1, retries + 1):
        try:
            with socket.create_connection((ip, port), timeout=timeout): return True
        except Exception:
            if attempt < retries: time.sleep(retry_delay)
    return False

# ═══════════════════════════════════════════════════════════════════════════════
# Redfish (server) session
# ═══════════════════════════════════════════════════════════════════════════════
class RedfishSession:
    def __init__(self, host):
        self.base = f"https://{host}"
        self.s    = requests.Session()
        self.s.headers.update({"OData-Version": "4.0"})
        self.token = None
        self.session_location = None

    def login(self):
        r = self.s.post(f"{self.base}/redfish/v1/SessionService/Sessions/",
                        json={"UserName": REDFISH_USER, "Password": REDFISH_PASS},
                        verify=False, timeout=30)
        r.raise_for_status()
        self.token = r.headers.get("X-Auth-Token")
        self.session_location = r.headers.get("Location")
        if not self.token or not self.session_location:
            raise RuntimeError("Redfish login ok but missing token/location")

    def get(self, path):
        r = self.s.get(f"{self.base}{path}",
                       headers={"X-Auth-Token": self.token},
                       verify=False, timeout=30)
        r.raise_for_status()
        return r.json()

    def logout(self):
        if not self.token or not self.session_location: return
        url = self.session_location if self.session_location.startswith("http") \
              else f"{self.base}{self.session_location}"
        try: self.s.delete(url, headers={"X-Auth-Token": self.token},
                           verify=False, timeout=10)
        except Exception: pass

def _resolve_server_name(rf, sys_data):
    serial = (sys_data.get("SerialNumber") or "").strip()
    model  = (sys_data.get("Model") or "").strip()
    hn = (sys_data.get("HostName") or "").strip()
    if hn and hn.lower() not in ("", "localhost", "computer system"):
        return hn
    try:
        mgr_col  = rf.get("/redfish/v1/Managers/")
        mgr      = rf.get(mgr_col["Members"][0]["@odata.id"])
        hp       = (mgr.get("Oem") or {})
        hp       = hp.get("Hp") or hp.get("Hpe") or {}
        srv_name = (hp.get("ServerName") or "").strip()
        if srv_name and srv_name.lower() not in ("", "computer system"):
            return srv_name
        ilo_name = (hp.get("iLOName") or mgr.get("HostName") or "").strip()
        if ilo_name and ilo_name.lower() not in ("", "manager", "ilo"):
            return ilo_name
    except Exception: pass
    asset = (sys_data.get("AssetTag") or "").strip()
    if asset and asset.lower() not in ("", "unknown"): return asset
    ip = rf.base.replace("https://", "").replace("http://", "")
    normalized = normalize_model(model, SERVER_MODEL_MAP) if model else None
    if normalized and normalized != "Unknown" and serial:
        return f"{normalized}-{serial}"
    if normalized and normalized != "Unknown":
        return f"{normalized}-{ip}"
    if serial: return f"HPE-{serial}"
    return f"HPE-{ip}"

def probe_redfish(ip, retries=3, retry_delay=5):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, REDFISH_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        host = f"{ip}:{REDFISH_PORT}"
        try:
            rf = RedfishSession(host)
            rf.login()
            try:
                root   = rf.get("/redfish/v1/")
                syscol = rf.get(root["Systems"]["@odata.id"])
                sys    = rf.get(syscol["Members"][0]["@odata.id"])
                name   = _resolve_server_name(rf, sys)
                return {
                    "ip":           ip,
                    "host":         host,
                    "serial":       sys.get("SerialNumber"),
                    "model":        sys.get("Model"),
                    "hostname":     name,
                    "manufacturer": sys.get("Manufacturer") or "HPE",
                }
            finally:
                rf.logout()
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# Storage session
# ═══════════════════════════════════════════════════════════════════════════════
class StorageSession:
    API_PREFIX = "/api/"

    def __init__(self, ip, port=443):
        self.ip = ip
        self.base = f"https://{ip}:{port}"
        self.session = requests.Session()
        self.session.verify = False
        self.session_key = None

    def _credential_hash(self, hash_type):
        cred = f"{STORAGE_USER}_{STORAGE_PASS}".encode()
        if hash_type == "md5":
            return hashlib.md5(cred).hexdigest()
        return hashlib.sha256(cred).hexdigest()

    def login(self):
        errors = []
        for hash_type in (STORAGE_AUTH_HASH, "sha256", "md5"):
            if hash_type in errors: continue
            try:
                xml = self._request(f"login/{self._credential_hash(hash_type)}")
                status = self._response_status(xml)
                self.session_key = status["response"]
                self.session.cookies.set("wbisessionkey", self.session_key)
                self.session.cookies.set("wbiusername", STORAGE_USER)
                return
            except Exception as exc:
                errors.append(hash_type)
                last_error = exc
        raise RuntimeError(f"Storage login failed for {self.ip}: {last_error}")

    def logout(self):
        if not self.session_key: return
        try: self._request("exit")
        except Exception: pass
        finally: self.session_key = None

    def _headers(self):
        headers = {"dataType": "api"}
        if self.session_key:
            headers["sessionKey"] = self.session_key
        return headers

    def _quick_request(self, path):
        url = f"{self.base}{self.API_PREFIX}{path.lstrip('/')}"
        try:
            r = self.session.get(url, headers={"dataType": "api"}, verify=False, timeout=5)
            if r.status_code != 200:
                return None
            return ET.fromstring(r.text)
        except Exception:
            return None

    def quick_probe(self):
        """Fast check without login – is this a storage XML API?"""
        xml = self._quick_request("login/check")
        if xml is not None:
            return True
        xml = self._quick_request("show/system")
        if xml is not None:
            return True
        return False

    def _request(self, path, method="GET"):
        url = f"{self.base}{self.API_PREFIX}{path.lstrip('/')}"
        r = self.session.request(method, url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        if r.text.strip().startswith("*"):
            raise RuntimeError(f"STORAGE_RATE_LIMIT:{r.text.strip()}")
        try:
            return ET.fromstring(r.text)
        except ET.ParseError as exc:
            raise RuntimeError(f"Invalid XML from {url}: {exc}") from exc

    @staticmethod
    def _response_status(xml_root):
        status = xml_root.find("./OBJECT[@name='status']")
        if status is None:
            raise RuntimeError("Storage response missing status object")
        props = {p.get("name"): (p.text or "").strip() for p in status.findall("PROPERTY")}
        if props.get("response-type", "").lower() != "success":
            raise RuntimeError(props.get("response") or props.get("response-type") or "Storage API error")
        return props

    def show(self, command, retries=4, retry_delay=5):
        for attempt in range(1, retries + 1):
            try:
                xml = self._request(f"show/{command}")
                # Check status but don't discard the response if it's just an
                # Info-level message (e.g. "Rates may vary"). The XML may still
                # contain the requested data alongside the info status object.
                # Only raise on Error-level status or when there are no data
                # objects at all.
                status_props = {}
                status_obj = xml.find("./OBJECT[@name='status']")
                if status_obj is not None:
                    status_props = {p.get("name"): (p.text or "").strip()
                                    for p in status_obj.findall("PROPERTY")}
                resp_type = status_props.get("response-type", "").lower()

                if resp_type == "error":
                    raise RuntimeError(status_props.get("response") or "Storage API error")

                if resp_type == "info":
                    # Info-level (e.g. "Rates may vary") -- the data may still
                    # be present. Parse objects and return them if we got any.
                    objects = self._parse_objects(xml)
                    if objects:
                        return objects
                    # No data objects -- treat as rate-limit and retry
                    raise RuntimeError(f"STORAGE_RATE_LIMIT:{status_props.get('response', '')}")

                # Success or unknown status -- parse and return
                return self._parse_objects(xml)

            except RuntimeError as exc:
                if "STORAGE_RATE_LIMIT" in str(exc):
                    if attempt < retries:
                        log("WARN", f"  Rate-limit on show {command} ({self.ip}), "
                                    f"retry {attempt}/{retries - 1} in {retry_delay}s ...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                raise

    @staticmethod
    def _parse_objects(xml_root):
        objects = []
        for obj in xml_root.findall("OBJECT"):
            basetype = obj.get("basetype")
            if not basetype or basetype == "status": continue
            props = {"basetype": basetype, "name": obj.get("name"), "oid": obj.get("oid")}
            for prop in obj.findall("PROPERTY"):
                props[prop.get("name")] = (prop.text or "").strip()
            objects.append(props)
        return objects

def probe_storage(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, STORAGE_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        storage = StorageSession(ip, STORAGE_PORT)
        if not storage.quick_probe():
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        try:
            storage.login()
            system_rows = storage.show("system")
            version_rows = storage.show("versions")
            if not system_rows: raise RuntimeError("empty system response")

            system = system_rows[0]
            serial = system.get("serial-number") or system.get("midplane-serial-number")
            product = system.get("product-id") or system.get("vendor-name") or "Storage"
            system_name = system.get("system-name") or system.get("system-contact") or f"storage-{ip.replace('.', '-')}"
            firmware = None
            for row in version_rows:
                fw = row.get("bundle-version") or row.get("sc-firmware") or row.get("firmware-version")
                if fw: firmware = fw; break

            return {
                "ip":           ip,
                "serial":       serial,
                "model":        normalize_model(product, STORAGE_MODEL_MAP) or product,
                "hostname":     system_name.strip(),
                "manufacturer": system.get("vendor-name") or DEFAULT_MFR,
                "health":       system.get("health"),
                "firmware":     firmware,
            }
        except Exception:
            if attempt < retries:
                time.sleep(retry_delay); continue
            return None
        finally:
            try: storage.logout()
            except Exception: pass
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# unified scanner
# ═══════════════════════════════════════════════════════════════════════════════
def scan_all():
    all_found = {"servers": [], "storage": [], "san_switches": []}

    bmc_ips = expand_ranges(BMC_RANGES)
    log("INFO", f"Scanning {len(bmc_ips)} IPs across {len(BMC_RANGES)} BMC ranges ...")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(probe_redfish, ip): ip for ip in bmc_ips}
        for f in as_completed(futures):
            r = f.result()
            if r:
                log("INFO", f"  + SERVER {r['ip']}  {r['model']}  s/n={r['serial']}")
                all_found["servers"].append(r)
    log("INFO", f"Server scan done: {len(all_found['servers'])} found.")

    server_ips = {h["ip"] for h in all_found["servers"]}
    all_storage_ips = expand_ranges(STORAGE_RANGES)
    storage_ips = [ip for ip in all_storage_ips if ip not in server_ips]
    skipped = len(all_storage_ips) - len(storage_ips)
    if skipped:
        log("INFO", f"Skipped {skipped} IP(s) in storage ranges already found as servers.")

    if storage_ips:
        log("INFO", f"Scanning {len(storage_ips)} IPs for storage ...")
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(probe_storage, ip): ip for ip in storage_ips}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    log("INFO", f"  + STORAGE {r['ip']}  {r['model']}  s/n={r['serial']}")
                    all_found["storage"].append(r)
        log("INFO", f"Storage scan done: {len(all_found['storage'])} found.")
    else:
        log("WARN", "No storage ranges to scan (all excluded or none configured).")

    # ── SAN switches (SSH on port 22) ────────────────────────────────────────
    used_ips = server_ips | {h["ip"] for h in all_found["storage"]}
    all_san_ips = expand_ranges(SAN_RANGES)
    san_ips = [ip for ip in all_san_ips if ip not in used_ips]
    skipped_san = len(all_san_ips) - len(san_ips)
    if skipped_san:
        log("INFO", f"Skipped {skipped_san} IP(s) in SAN ranges already found as server/storage.")
    if san_ips:
        log("INFO", f"Scanning {len(san_ips)} IPs for SAN switches (SSH) ...")
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(probe_san_switch, ip): ip for ip in san_ips}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    log("INFO", f"  + SAN {r['ip']}  {r.get('model')}  wwn={r.get('wwn')}")
                    all_found["san_switches"].append(r)
        log("INFO", f"SAN switch scan done: {len(all_found['san_switches'])} found.")
    else:
        log("WARN", "No SAN switch ranges to scan (all excluded or none configured).")

    return all_found

# ═══════════════════════════════════════════════════════════════════════════════
# NetBox CRUD helpers
# ═══════════════════════════════════════════════════════════════════════════════
def _get_or_create(endpoint, lookup, create):
    obj = endpoint.get(**lookup)
    if obj: return obj.id
    return endpoint.create(create).id

def get_or_create_manufacturer(name):
    if not name: return None
    name = name.strip()
    api = get_netbox()
    # Primary lookup by name (case-insensitive in NetBox)
    m = api.dcim.manufacturers.get(name=name)
    if m: return m.id
    # Secondary lookup by slug (handles pre-existing manufacturers with
    # different casing or a manually-set slug)
    slug = slugify(name)
    try:
        m = api.dcim.manufacturers.get(slug=slug)
        if m: return m.id
    except Exception: pass
    # Try to create; if slug collides, fall back to a suffixed slug
    for attempt in range(3):
        try:
            return api.dcim.manufacturers.create(
                {"name": name, "slug": slug if attempt == 0 else f"{slug}-{attempt+1}"}).id
        except Exception as e:
            if "already exists" in str(e) and attempt < 2:
                continue
            # Last resort: re-query by name (race conditions, etc.)
            m = api.dcim.manufacturers.get(name=name)
            if m: return m.id
            raise

def get_or_create_device_type(model, mfr_id, model_map=None):
    m = normalize_model(model, model_map) or model or "Unknown"
    return _get_or_create(get_netbox().dcim.device_types, {"model": m},
                          {"model": m, "slug": slugify(m), "manufacturer": mfr_id})

def get_or_create_role(name, color="9e9e9e"):
    api = get_netbox()
    r = api.dcim.device_roles.get(name=name)
    if r: return r.id
    r = api.dcim.device_roles.get(slug=slugify(name))
    if r: return r.id
    return api.dcim.device_roles.create(
        {"name": name, "slug": slugify(name), "color": color}).id

_INVENTORY_ROLE_CACHE = {}
def get_or_create_inventory_role(name, color="9e9e9e"):
    """Inventory-item-role IDs are NOT portable between NetBox instances —
    unlike device roles, they can't safely be hardcoded, so resolve by name
    (creating the role if it doesn't exist yet) and cache the result."""
    if name in _INVENTORY_ROLE_CACHE:
        return _INVENTORY_ROLE_CACHE[name]
    api = get_netbox()
    r = api.dcim.inventory_item_roles.get(name=name)
    if not r:
        r = api.dcim.inventory_item_roles.get(slug=slugify(name))
    if not r:
        r = api.dcim.inventory_item_roles.create(
            {"name": name, "slug": slugify(name), "color": color})
    _INVENTORY_ROLE_CACHE[name] = r.id
    return r.id

def get_or_create_site(name):
    return _get_or_create(get_netbox().dcim.sites, {"name": name},
                          {"name": name, "slug": slugify(name), "status": "active"})

def find_device(serial, role_name=None):
    """Search by serial only — custom field filters are unreliable in this NetBox."""
    if _invalid_serial(serial):
        return None
    api = get_netbox()
    results = list(api.dcim.devices.filter(serial=serial.strip()))
    if not results:
        return None
    if role_name:
        match = [d for d in results if d.role and d.role.name == role_name]
        return match[0] if match else None
    return results[0]

# ═══════════════════════════════════════════════════════════════════════════════
# device ensure / mark offline
# ═══════════════════════════════════════════════════════════════════════════════
def _device_name(probe, prefix="server"):
    hn = probe.get("hostname") or f"{prefix}-{probe['ip'].replace('.', '-')}"
    return hn.strip()[:64]

def ensure_server_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "HPE")
    role_id = get_or_create_role(SERVER_ROLE)
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, SERVER_MODEL_MAP)
    name = _device_name(probe)
    api = get_netbox()
    dev = find_device(serial, role_name=SERVER_ROLE)
    # Secondary: find by name+site+role
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = next((c for c in cands if not (c.custom_fields or {}).get("storage_ip")), None)
        if dev: log("INFO", f"  Found server by name+site: {name} (id={dev.id})")
    if dev:
        api.dcim.devices.update([{
            "id": dev.id, "name": name, "status": "active",
            "site": site_id, "device_type": dtype_id, "role": role_id,
            "custom_fields": {"bmc_ip": probe["ip"], "redfish_enabled": True},
            **({"serial": serial} if not _invalid_serial(serial) else {}),
        }])
        log("INFO", f"  Server updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create({
        "name": name, "device_type": dtype_id, "role": role_id,
        "site": site_id, "serial": serial if not _invalid_serial(serial) else "",
        "status": "active",
        "custom_fields": {"bmc_ip": probe["ip"], "redfish_enabled": True},
    })
    log("INFO", f"  Server created: {name} (id={new.id})")
    return new.id

def ensure_storage_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or DEFAULT_MFR)
    role_id = get_or_create_role(STORAGE_ROLE, "2196f3")
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, STORAGE_MODEL_MAP)
    name = _device_name(probe, prefix="storage")
    api = get_netbox()
    dev = find_device(serial, role_name=STORAGE_ROLE)
    # Secondary: find by name+site+role (storage names unique per site)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = next((c for c in cands if not (c.custom_fields or {}).get("bmc_ip")), None)
        if dev: log("INFO", f"  Found storage by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id,
        "custom_fields": {
            "storage_ip":       probe["ip"],
            "storage_enabled":  True,
            "storage_health":   probe.get("health"),
            "storage_firmware": probe.get("firmware"),
            "storage_model":    probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload, "role": role_id}])
        log("INFO", f"  Storage updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create({**payload, "role": role_id})
    log("INFO", f"  Storage created: {name} (id={new.id})")
    return new.id

def mark_server_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"redfish_enabled": False},
        }])
        log("WARN", f"  Server marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark server offline {dev_name}: {e}")

def mark_storage_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"storage_enabled": False},
        }])
        log("WARN", f"  Storage marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark storage offline {dev_name}: {e}")

# ── Consecutive-failure tracking (prevents flapping) ─────────────────────────
# A device must fail to appear in the scan for OFFLINE_THRESHOLD consecutive
# runs before being marked offline. The counter persists across scheduled runs
# in process memory and resets to 0 the moment the device is seen again.
OFFLINE_THRESHOLD = int(os.getenv("OFFLINE_THRESHOLD", "2"))
_scan_fail_counts = {}   # {ip: consecutive_miss_count}

def _check_offline(ip, live_ips, dev_id, dev_name, mark_fn, label):
    """Shared logic: increment miss counter if absent, mark offline only
    when the threshold is reached. Reset counter when the device is present."""
    if ip in live_ips:
        if ip in _scan_fail_counts:
            _scan_fail_counts.pop(ip, None)
        return
    misses = _scan_fail_counts.get(ip, 0) + 1
    _scan_fail_counts[ip] = misses
    if misses >= OFFLINE_THRESHOLD:
        mark_fn(dev_id, dev_name)
        _scan_fail_counts.pop(ip, None)   # reset after marking
    else:
        log("INFO", f"  {label} {dev_name} not seen this run "
            f"(miss {misses}/{OFFLINE_THRESHOLD}) -- keeping active")

# ═══════════════════════════════════════════════════════════════════════════════
# inventory collection – Redfish (server)
# ═══════════════════════════════════════════════════════════════════════════════
def rf_collect_inventory(host):
    rf = RedfishSession(host)
    rf.login()
    try:
        root      = rf.get("/redfish/v1/")
        syscol    = rf.get(root["Systems"]["@odata.id"])
        sys       = rf.get(syscol["Members"][0]["@odata.id"])
        sys_odata = sys.get("@odata.id")
        oem_data  = _get_oem(sys)

        inventory = {}

        add_item = _make_add_item(inventory)

        # CPU
        cpu_model = cpu_sockets = cpu_cores = cpu_threads = None
        ps = sys.get("ProcessorSummary") or {}
        cpu_model   = _pick(ps, ["Model"])
        cpu_sockets = _to_int(ps.get("Count"))
        cpu_cores   = _to_int(ps.get("CoreCount"))
        cpu_threads = _to_int(ps.get("ThreadCount"))

        procs_link = (sys.get("Processors") or {}).get("@odata.id") \
                     if isinstance(sys.get("Processors"), dict) else None
        if procs_link:
            models, sockets, cores, threads = [], 0, 0, 0
            for m in rf.get(procs_link).get("Members", []):
                p = rf.get(m["@odata.id"])
                if (p.get("Status") or {}).get("State") == "Absent": continue
                sockets += 1
                if p.get("Model"): models.append(p["Model"])
                cores   += _to_int(p.get("TotalCores"))   or 0
                threads += _to_int(p.get("TotalThreads")) or 0
                add_item(
                    name=name_cpu(p),
                    manufacturer=p.get("Manufacturer"),
                    part_number=None,
                    serial=_pick(p, ["SerialNumber"]),
                    description=f"Model={p.get('Model')} Cores={p.get('TotalCores')} Threads={p.get('TotalThreads')}",
                    role_id=get_or_create_inventory_role("CPU"))
            cpu_sockets = cpu_sockets or (sockets or None)
            cpu_cores   = cpu_cores   or (cores   or None)
            cpu_threads = cpu_threads or (threads or None)
            if models and not cpu_model: cpu_model = max(set(models), key=models.count)

        # RAM
        ram_gib = None
        ms = sys.get("MemorySummary") or {}
        if ms.get("TotalSystemMemoryGiB") is not None:
            try: ram_gib = int(round(float(ms["TotalSystemMemoryGiB"])))
            except Exception: pass

        mem_link = (sys.get("Memory") or {}).get("@odata.id") \
                   if isinstance(sys.get("Memory"), dict) else None
        if mem_link:
            total_mib = 0
            for m in rf.get(mem_link).get("Members", []):
                mm = rf.get(m["@odata.id"])
                if (mm.get("Status") or {}).get("State") == "Absent": continue
                cap = _to_int(mm.get("CapacityMiB"))
                if cap: total_mib += cap
                add_item(
                    name=name_ram(mm),
                    manufacturer=mm.get("Manufacturer"),
                    part_number=_pick(mm, ["PartNumber","PartNumberString"]),
                    serial=_pick(mm, ["SerialNumber","SerialNumberString"]),
                    description=f"Model={mm.get('Model')} CapacityMiB={mm.get('CapacityMiB')} "
                                f"SpeedMHz={mm.get('OperatingSpeedMhz')} Type={mm.get('MemoryDeviceType')}",
                    role_id=get_or_create_inventory_role("Memory"))
            if ram_gib is None and total_mib:
                ram_gib = int(round(total_mib / 1024))

        # Storage (Redfish)
        disk_total_bytes = 0
        drive_idx = 0

        stor_link = (sys.get("Storage") or {}).get("@odata.id") \
                    if isinstance(sys.get("Storage"), dict) else None
        if not stor_link:
            try:
                cu = _chassis_url(sys)
                if cu:
                    ch = rf.get(cu)
                    stor_link = (ch.get("Storage") or {}).get("@odata.id") \
                                if isinstance(ch.get("Storage"), dict) else None
            except Exception: pass

        if stor_link:
            for sm in rf.get(stor_link).get("Members", []):
                stor = rf.get(sm["@odata.id"])
                cr = stor.get("StorageControllers") or stor.get("Controllers")
                ctrls = []
                if isinstance(cr, list): ctrls = cr
                elif isinstance(cr, dict):
                    cl = cr.get("@odata.id") or cr.get("href")
                    if cl:
                        for m2 in (rf.get(cl).get("Members") or []):
                            u = m2.get("@odata.id") or m2.get("href")
                            if u:
                                try: ctrls.append(rf.get(u))
                                except Exception: pass
                for ctrl in ctrls:
                    if not isinstance(ctrl, dict): continue
                    if (ctrl.get("Status") or {}).get("State") == "Absent": continue
                    add_item(
                        name=f"Controller-{_get_location(ctrl) or 'CTRL'}",
                        manufacturer=ctrl.get("Manufacturer"),
                        part_number=_pick(ctrl, ["PartNumber","SKU","SparePartNumber","ProductId"]),
                        serial=_pick(ctrl, ["SerialNumber"]),
                        description=f"Model={_pick(ctrl,['Model','ProductName','Name'])} "
                                    f"Firmware={_pick(ctrl,['FirmwareVersion','Version'])}",
                        role_id=get_or_create_inventory_role("Controller"))
                for d in (stor.get("Drives") or []):
                    drv = rf.get(d["@odata.id"])
                    if not isinstance(drv, dict): continue
                    if (drv.get("Status") or {}).get("State") == "Absent": continue
                    cap = _capacity_to_bytes(drv)
                    if cap: disk_total_bytes += cap
                    role_id = get_or_create_inventory_role("SSD") if is_ssd(drv) \
                          else get_or_create_inventory_role("HDD")
                    add_item(
                        name=name_disk(drv),
                        manufacturer=drv.get("Manufacturer"),
                        part_number=_pick(drv, ["PartNumber","Model"]),
                        serial=_pick(drv, ["SerialNumber"]),
                        description=f"Model={drv.get('Model')} Capacity={drv.get('CapacityBytes')} "
                                    f"MediaType={drv.get('MediaType')} Protocol={drv.get('Protocol')}",
                        role_id=role_id)
                    drive_idx += 1

        # HPE SmartStorage fallback (Gen9) — only when Redfish yielded no drives
        if drive_idx == 0:
            sl_obj = (oem_data.get("Links") or {}).get("SmartStorage") or {} \
                     if isinstance(oem_data.get("Links"), dict) else {}
            smart_url = sl_obj.get("@odata.id") or sl_obj.get("href") \
                        if isinstance(sl_obj, dict) else None
            if not smart_url and sys_odata:
                smart_url = sys_odata.rstrip("/") + "/SmartStorage/"
            if smart_url:
                try:
                    smart = rf.get(smart_url)
                    ac_obj = (smart.get("Links") or {}).get("ArrayControllers") or {}
                    cl = ac_obj.get("@odata.id") or ac_obj.get("href") \
                         or smart_url.rstrip("/") + "/ArrayControllers/"
                    for cm in rf.get(cl).get("Members", []):
                        ctrl = rf.get(cm["@odata.id"])
                        add_item(
                            name=f"Controller-{_get_location(ctrl) or 'CTRL'}",
                            manufacturer=ctrl.get("Manufacturer"),
                            part_number=_pick(ctrl, ["PartNumber","SKU","SparePartNumber","ProductId"]),
                            serial=_pick(ctrl, ["SerialNumber"]),
                            description=f"Model={_pick(ctrl,['Model','ProductName','Name'])} "
                                        f"Firmware={_pick(ctrl,['FirmwareVersion','Version'])}",
                            role_id=get_or_create_inventory_role("Controller"))
                        pd_info = ctrl.get("PhysicalDrives") or (ctrl.get("Links") or {}).get("PhysicalDrives") or {}
                        if isinstance(pd_info, dict):
                            pu = pd_info.get("@odata.id") or pd_info.get("href")
                            members = rf.get(pu).get("Members") or [] if pu else []
                        elif isinstance(pd_info, list): members = pd_info
                        else: members = []
                        for pdm in members:
                            u = pdm.get("@odata.id") or pdm.get("href")
                            if not u: continue
                            drv = rf.get(u)
                            cap = _capacity_to_bytes(drv)
                            if cap: disk_total_bytes += cap
                            role_id = get_or_create_inventory_role("SSD") \
                                      if is_ssd(drv) else get_or_create_inventory_role("HDD")
                            add_item(
                                name=name_disk(drv),
                                manufacturer=drv.get("Manufacturer"),
                                part_number=drv.get("PartNumber") or drv.get("Model"),
                                serial=drv.get("SerialNumber"),
                                description=f"Model={drv.get('Model')} CapacityGB={drv.get('CapacityGB')} "
                                            f"MediaType={drv.get('MediaType')}",
                                role_id=role_id)
                            drive_idx += 1
                except Exception: pass

        # Power Supplies
        try:
            cu = _chassis_url(sys)
            if cu:
                chassis = rf.get(cu)
                pl = (chassis.get("Power") or {}).get("@odata.id") \
                     if isinstance(chassis.get("Power"), dict) else None
                if pl:
                    for psu in rf.get(pl).get("PowerSupplies", []):
                        if not isinstance(psu, dict): continue
                        if (psu.get("Status") or {}).get("State") == "Absent": continue
                        add_item(
                            name=name_psu(psu),
                            manufacturer=psu.get("Manufacturer"),
                            part_number=_pick(psu, ["PartNumber","SparePartNumber","Model"]),
                            serial=_pick(psu, ["SerialNumber"]),
                            description=f"Model={_pick(psu,['Model','Name'])} "
                                        f"LineInputVoltage={psu.get('LineInputVoltage')} "
                                        f"PowerCapacityW={psu.get('PowerCapacityWatts')}",
                            role_id=get_or_create_inventory_role("PSU"))
        except Exception: pass

        # Battery Gen9
        for bat in (oem_data.get("Battery") or []):
            if not isinstance(bat, dict): continue
            if not bat.get("SerialNumber"): continue
            idx = bat.get("Index") or "1"
            add_item(
                name=f"Battery {idx}",
                manufacturer="HPE",
                part_number=bat.get("Model") or bat.get("Spare"),
                serial=bat["SerialNumber"],
                description=f"Model={bat.get('ProductName')} "
                            f"FirmwareVersion={bat.get('FirmwareVersion')} "
                            f"Condition={bat.get('Condition')}",
                role_id=get_or_create_inventory_role("Battery"))

        # Battery Gen10
        try:
            cu = _chassis_url(sys)
            if cu:
                chassis_hpe = _get_oem(rf.get(cu)) or {}
                for bat in (chassis_hpe.get("SmartStorageBattery") or []):
                    if not isinstance(bat, dict): continue
                    if not bat.get("SerialNumber"): continue
                    idx = bat.get("Index") or "1"
                    add_item(
                        name=f"Battery {idx}",
                        manufacturer="HPE",
                        part_number=bat.get("Model") or bat.get("SparePartNumber"),
                        serial=bat["SerialNumber"],
                        description=f"Model={bat.get('ProductName','Smart Storage Battery')} "
                                    f"FirmwareVersion={bat.get('FirmwareVersion')} "
                                    f"MaximumCapWatts={bat.get('MaximumCapWatts')} "
                                    f"ChargeLevel={bat.get('ChargeLevelPercent')}%",
                        role_id=get_or_create_inventory_role("Battery"))
        except Exception: pass

        # Network Adapters
        try:
            uefi_to_pci = {}
            try:
                pci_col = rf.get(sys_odata.rstrip("/") + "/PCIDevices/")
                items = pci_col.get("Items") or []
                if not items:
                    for m in (pci_col.get("Members") or []):
                        if "Name" in m: items.append(m)
                        else:
                            try: items.append(rf.get(m["@odata.id"]))
                            except Exception: pass
                for item in items:
                    if isinstance(item, dict) and item.get("UEFIDevicePath"):
                        uefi_to_pci[item["UEFIDevicePath"]] = item
            except Exception: pass

            for m in (rf.get(sys_odata.rstrip("/") + "/NetworkAdapters/").get("Members") or []):
                try:
                    adapter = rf.get(m["@odata.id"])
                    if not isinstance(adapter, dict): continue
                    serial = adapter.get("SerialNumber")
                    if not serial: continue
                    ports = adapter.get("PhysicalPorts") or []
                    pci_info = None
                    for port in ports:
                        pp = port.get("UEFIDevicePath")
                        if pp and pp in uefi_to_pci: pci_info = uefi_to_pci[pp]; break
                    if not pci_info:
                        ap = adapter.get("UEFIDevicePath")
                        if ap and ap in uefi_to_pci: pci_info = uefi_to_pci[ap]
                    aname = adapter.get("Name") or "NIC"
                    fw = (adapter.get("Firmware") or {}).get("Current", {}).get("VersionString")
                    macs = " ".join(p.get("MacAddress","") for p in ports[:2] if p.get("MacAddress"))
                    add_item(
                        name=name_nic(aname, pci_info),
                        manufacturer="HPE",
                        part_number=adapter.get("PartNumber"),
                        serial=serial,
                        description=f"Model={aname} FW={fw} MACs={macs}",
                        role_id=get_or_create_inventory_role("NIC"))
                except Exception: pass
        except Exception: pass

        # PCIe FRUs Gen10 (with real SerialNumber)
        try:
            pci_link = None
            pl_obj = (oem_data.get("Links") or {}).get("PCIDevices") or {} \
                     if isinstance(oem_data.get("Links"), dict) else {}
            pci_link = pl_obj.get("@odata.id") or pl_obj.get("href") \
                       if isinstance(pl_obj, dict) else None
            if not pci_link:
                try:
                    cu = _chassis_url(sys)
                    if cu:
                        ch = rf.get(cu)
                        pcie = ch.get("PCIeDevices") or {}
                        pci_link = pcie.get("@odata.id") or pcie.get("href") \
                                   if isinstance(pcie, dict) else None
                except Exception: pass
            if pci_link:
                for m in (rf.get(pci_link).get("Members") or []):
                    try:
                        dev = rf.get(m["@odata.id"])
                        serial = dev.get("SerialNumber") if isinstance(dev, dict) else None
                        if not serial: continue
                        dname = dev.get("ProductName") or dev.get("Name") or "PCIe"
                        role_id = get_or_create_inventory_role("HBA") \
                                  if any(k in dname for k in ("HBA","FC","Fibre")) \
                                  else get_or_create_inventory_role("NIC")
                        add_item(
                            name=dname[:64],
                            manufacturer=dev.get("Manufacturer") or sys.get("Manufacturer"),
                            part_number=dev.get("ProductPartNumber") or dev.get("PartNumber"),
                            serial=serial,
                            description=f"ProductVersion={dev.get('ProductVersion')} "
                                        f"FirmwareVersion={dev.get('FirmwareVersion')}",
                            role_id=role_id)
                    except Exception: pass
        except Exception: pass

        # HBA pseudo-serial (Gen9 iLO4)
        try:
            pci_col = rf.get(sys_odata.rstrip("/") + "/PCIDevices/")
            pci_items = pci_col.get("Items") or []
            if not pci_items:
                for m in (pci_col.get("Members") or []):
                    if "Name" in m: pci_items.append(m)
                    else:
                        try: pci_items.append(rf.get(m["@odata.id"]))
                        except Exception: pass

            for item in pci_items:
                if not isinstance(item, dict): continue
                device_location = item.get("DeviceLocation") or ""
                name_str        = item.get("Name") or ""
                structured_name = item.get("StructuredName") or ""
                device_type     = item.get("DeviceType") or ""

                if "Embedded" in device_location or "LOM" in device_location: continue
                if device_type in ("SATA Controller",): continue

                is_hba = any(k in name_str for k in
                             ("HBA","FC","Fibre","Emulex","QLogic","Brocade","SN1100","SN1200"))
                if not is_hba: continue
                if not structured_name: continue

                subsystem_id  = item.get("SubsystemDeviceID") or "0"
                pseudo_serial = f"{structured_name}-{subsystem_id}"

                already = any(device_location.replace("PCI-E ","").replace(" ","") in v.get("name","")
                              for s, v in inventory.items() if not s.startswith("PCI."))
                if already: continue

                fw_version = None
                item_uefi  = item.get("UEFIDevicePath") or ""
                try:
                    fw_inv = rf.get(sys_odata.rstrip("/") + "/FirmwareInventory/")
                    for key, entries in (fw_inv.get("Current") or {}).items():
                        if not isinstance(entries, list): continue
                        for entry in entries:
                            if item_uefi and item_uefi in (entry.get("UEFIDevicePaths") or []):
                                fw_version = entry.get("VersionString"); break
                        if fw_version: break
                except Exception: pass

                add_item(
                    name=name_hba(name_str, device_location),
                    manufacturer=sys.get("Manufacturer") or "HPE",
                    part_number=None,
                    serial=pseudo_serial,
                    description=f"Model={name_str} Slot={device_location} "
                                f"FW={fw_version} (pseudo-serial: no serial via iLO4)",
                    role_id=get_or_create_inventory_role("HBA"))
        except Exception: pass

        disk_total_gib = gib_from_bytes(disk_total_bytes) if disk_total_bytes else None
        return {
            "summary": {
                "model":          sys.get("Model"),
                "serial":         sys.get("SerialNumber"),
                "power_state":    sys.get("PowerState"),
                "bios_version":   sys.get("BiosVersion"),
                "cpu_model":      cpu_model,
                "cpu_sockets":    cpu_sockets,
                "cpu_cores":      cpu_cores,
                "cpu_threads":    cpu_threads,
                "ram_gib":        ram_gib,
                "disk_total_gib": disk_total_gib,
            },
            "inventory": inventory,
        }
    finally:
        rf.logout()


# ═══════════════════════════════════════════════════════════════════════════════
# inventory collection – storage
# ═══════════════════════════════════════════════════════════════════════════════
def storage_collect_inventory(ip):
    storage = StorageSession(ip, STORAGE_PORT)
    storage.login()
    time.sleep(5)
    try:
        inventory = {}
        disk_total_bytes = 0
        disk_count = 0

        add_item = _make_add_item(inventory)

        show_commands = [
            ("controllers",    "controllers",    _collect_controller_storage),
            ("power-supplies", "power-supplies", _collect_psu_storage),
            ("frus",           "enclosure-fru",  _collect_fru_storage),
            ("disks",          None,             _collect_disk_storage),
        ]

        for command, expected_type, collector in show_commands:
            rows = None
            if command == "disks":
                # ── Enrichment context ────────────────────────────────────────
                # MSA 2040 firmware permanently rate-limits "show disks", so
                # model/size/firmware are NOT available via the API. We enrich
                # the disk-statistics rows with inferable fields from other
                # endpoints:
                #   - drive-bus-type  from show enclosures (controller field)
                #   - array-drive-type from show disk-groups (per RAID group)
                #   - SSD vs HDD      inferred from disk-group name (SSD/HDD)
                enriched_drive_bus = None
                try:
                    enc_rows = storage.show("enclosures")
                    for er in enc_rows:
                        if er.get("basetype") == "controllers" and er.get("drive-bus-type"):
                            enriched_drive_bus = er.get("drive-bus-type")
                            break
                    if enriched_drive_bus:
                        log("INFO", f"    inferred drive-bus-type={enriched_drive_bus} from enclosures")
                except Exception as exc:
                    log("WARN", f"  show enclosures for drive-bus-type failed: {exc}")

                # Fetch disk-groups to infer per-disk type (SAS/SSD) from
                # group names and array-drive-type fields.
                disk_group_types = {}   # {pool-dg-name: {type, raid, bus}}
                try:
                    dg_rows = storage.show("disk-groups")
                    for dg in dg_rows:
                        if dg.get("basetype") == "disk-groups":
                            dg_name = dg.get("name") or ""
                            dg_info = {
                                "array-drive-type": dg.get("array-drive-type"),
                                "raidtype": dg.get("raidtype"),
                                "diskcount": dg.get("diskcount"),
                                "name": dg_name,
                            }
                            disk_group_types[dg_name] = dg_info
                    log("INFO", f"    found {len(disk_group_types)} disk-groups for type inference")
                except Exception as exc:
                    log("WARN", f"  show disk-groups failed: {exc}")

                # Dual-source strategy for MSA 2040/2060 compatibility:
                #
                #   "show disks"           -- MSA 2060: per-disk rows with
                #                            size, model, firmware, drive-type.
                #                            MSA 2040: permanently rate-limited.
                #   "show disk-statistics"  -- MSA 2040: per-disk rows with
                #                            serial-number, location, durable-id,
                #                            I/O stats.  No size/model/firmware.
                #
                # We fetch BOTH (when available) and merge by serial-number
                # so each NetBox inventory item carries every field that either
                # endpoint exposes, plus inferred fields from enclosures/disk-groups.
                disk_rows_full = None     # from "show disks" (rich, may fail)
                disk_rows_stats = None    # from "show disk-statistics" (always works on MSA 2040)

                # 1. Try "show disks" (rich data). Tolerate rate-limit / failure.
                try:
                    disk_rows_full = storage.show("disks")
                    log("INFO", f"    disk command 'disks' succeeded on {ip}: {len(disk_rows_full)} rows")
                    if disk_rows_full:
                        bts = set(r.get("basetype") for r in disk_rows_full)
                        log("DEBUG", f"    disks: {len(disk_rows_full)} rows, basetypes={bts}")
                        sample = disk_rows_full[0]
                        log("DEBUG", f"    disks sample keys: {list(sample.keys())}")
                except Exception as exc:
                    msg = str(exc)
                    if "Rates may vary" in msg or "STORAGE_RATE_LIMIT" in msg:
                        log("WARN", f"  Rate-limit on show disks ({ip}), will use disk-statistics only")
                        try: storage.logout()
                        except Exception: pass
                        time.sleep(10)
                        try:
                            storage.login()
                            time.sleep(5)
                        except Exception as le:
                            log("WARN", f"  Re-login failed for {ip}: {le}")
                    else:
                        log("WARN", f"  show disks failed on {ip}: {exc}")
                    disk_rows_full = None

                # 2. Try "show disk-statistics" (always works on MSA 2040).
                #    Retries once if the session died during the disks attempt.
                for stat_attempt in range(2):
                    try:
                        disk_rows_stats = storage.show("disk-statistics")
                        log("INFO", f"    disk command 'disk-statistics' succeeded on {ip}: {len(disk_rows_stats)} rows")
                        if disk_rows_stats:
                            bts = set(r.get("basetype") for r in disk_rows_stats)
                            log("DEBUG", f"    disk-statistics: {len(disk_rows_stats)} rows, basetypes={bts}")
                            sample = disk_rows_stats[0]
                            log("DEBUG", f"    disk-statistics sample keys: {list(sample.keys())}")
                        break
                    except Exception as exc:
                        msg = str(exc)
                        if "Rates may vary" in msg or "STORAGE_RATE_LIMIT" in msg:
                            log("WARN", f"  Rate-limit on show disk-statistics ({ip}), retrying ...")
                            try: storage.logout()
                            except Exception: pass
                            time.sleep(10)
                            try:
                                storage.login(); time.sleep(5)
                            except Exception as le:
                                log("WARN", f"  Re-login failed for {ip}: {le}")
                                break
                        else:
                            log("WARN", f"  show disk-statistics failed on {ip}: {exc}")
                            break

                # 3. Merge: build a {serial: merged_row} dict from both sources.
                #    Fields from "show disks" (rich) take precedence; fields
                #    from "disk-statistics" fill the gaps (serial, location,
                #    I/O stats, power-on-hours).
                merged_by_serial = {}
                # Start with disk-statistics (always available on MSA 2040)
                if disk_rows_stats:
                    for row in disk_rows_stats:
                        serial = (row.get("serial-number") or "").strip()
                        if serial:
                            merged_by_serial[serial] = dict(row)
                # Overlay "show disks" rows (richer data wins)
                if disk_rows_full:
                    for row in disk_rows_full:
                        serial = (row.get("serial-number") or "").strip()
                        if not serial:
                            continue
                        if serial in merged_by_serial:
                            # Merge: disks fields override, stats fields fill gaps
                            base = merged_by_serial[serial]
                            for k, v in row.items():
                                if v and not base.get(k):
                                    base[k] = v
                            # Ensure the richer basetype is used for filtering
                            base["basetype"] = row.get("basetype") or base.get("basetype")
                        else:
                            merged_by_serial[serial] = dict(row)

                if not merged_by_serial:
                    # Last resort: try disk-parameters (global params only,
                    # but some firmwares emit per-disk rows here too)
                    try:
                        dp_rows = storage.show("disk-parameters")
                        for row in dp_rows:
                            serial = (row.get("serial-number") or "").strip()
                            if serial:
                                merged_by_serial[serial] = dict(row)
                    except Exception as exc:
                        log("WARN", f"  show disk-parameters failed on {ip}: {exc}")

                # 4. Enrich merged rows with inferred fields from enclosures
                #    and disk-groups. This adds drive-bus-type and
                #    array-drive-type to rows that only have disk-statistics data.
                if enriched_drive_bus or disk_group_types:
                    for serial, row in merged_by_serial.items():
                        # Drive bus type (SAS) from controller
                        if enriched_drive_bus and not row.get("drive-bus-type"):
                            row["drive-bus-type"] = enriched_drive_bus
                        # If no drive-type/disk-type yet, try to infer from
                        # disk-group names. The location field (e.g. "1.1")
                        # doesn't map to disk-groups directly, but we can set
                        # a default type from the most common disk-group type.
                        if not row.get("drive-type") and not row.get("disk-type"):
                            # Use the first disk-group's array-drive-type as
                            # a reasonable default (most disks are SAS on MSA 2040)
                            for dg_name, dg_info in disk_group_types.items():
                                if "SSD" in dg_name.upper():
                                    row["inferred-ssd"] = True
                                elif "HDD" in dg_name.upper():
                                    row["inferred-ssd"] = False
                                if dg_info.get("array-drive-type") and not row.get("drive-type"):
                                    row["drive-type"] = dg_info["array-drive-type"]
                                break  # just use the first group as default

                rows = list(merged_by_serial.values()) if merged_by_serial else None
                if rows is None:
                    log("WARN", f"  All disk commands failed on {ip} -- disks will not be synced")
            else:
                try:
                    rows = storage.show(command)
                except Exception as exc:
                    log("WARN", f"  show {command} failed on {ip}: {exc}")

            if rows is None:
                continue

            actual_types = set(r.get("basetype") for r in rows)
            matched = 0
            for row in rows:
                bt = row.get("basetype") or ""
                if command == "disks":
                    # Accept any basetype that represents a physical disk:
                    #   MSA 2060: "drive"
                    #   MSA 2040: "disk-statistics", "disk-parameters"
                    bt_lower = bt.lower()
                    if "drive" not in bt_lower and "disk" not in bt_lower:
                        continue
                    # Skip global/non-per-disk rows (e.g. disk-parameters on
                    # MSA 2040 returns a single row of global params with no
                    # serial-number -- we only want rows that represent an
                    # actual disk with an identity).
                    if not (row.get("serial-number") or row.get("durable-id")):
                        continue
                elif expected_type and bt != expected_type:
                    continue
                added_bytes = collector(row, add_item)
                matched += 1
                if command == "disks" and added_bytes:
                    disk_total_bytes += added_bytes
                    disk_count += 1
            log("INFO", f"    show {command}: {len(rows)} rows, basetypes={actual_types}, matched={matched}")

        summary = {
            "serial":       None,
            "model":        None,
            "health":       None,
            "firmware":     None,
            "disk_count":   disk_count,
            "disk_total_gib": gib_from_bytes(disk_total_bytes),
        }

        try:
            system = storage.show("system")[0]
            summary["serial"] = system.get("serial-number")
            summary["model"] = normalize_model(system.get("product-id"), STORAGE_MODEL_MAP) or system.get("product-id")
            summary["health"] = system.get("health")
        except Exception: pass

        try:
            for row in storage.show("versions"):
                fw = row.get("bundle-version") or row.get("sc-firmware") or row.get("firmware-version")
                if fw: summary["firmware"] = fw; break
        except Exception: pass

        return {"summary": summary, "inventory": inventory}
    finally:
        storage.logout()


def _collect_disk_storage(row, add_item):
    # Collect every useful field a merged "show disks" + "show disk-statistics"
    # row can expose. On MSA 2040, size/model/firmware are unavailable (show
    # disks is permanently rate-limited), but drive-bus-type (SAS) and
    # array-drive-type are inferred from enclosures + disk-groups and injected
    # into the row by storage_collect_inventory before this is called.
    serial = row.get("serial-number")
    if not serial:
        return 0

    # Size / capacity -- try every known field name
    size_str = (row.get("size") or row.get("total-size")
                or row.get("formatted-size") or row.get("raw-size")
                or row.get("capacity") or row.get("disk-size"))
    size_num = (row.get("size-numeric") or row.get("total-size-numeric")
                or row.get("raw-size-numeric") or row.get("capacity-numeric"))
    cap = parse_storage_size_bytes(size_str, size_num)

    # Model / part number
    model = (row.get("model") or row.get("disk-description")
             or row.get("description") or row.get("product-id")
             or row.get("vendor-product-id"))

    # Manufacturer / vendor
    vendor = (row.get("vendor") or row.get("manufacturer")
              or row.get("vendor-name") or DEFAULT_MFR)

    # Firmware version
    firmware = (row.get("firmware-version") or row.get("firmware")
                or row.get("drive-firmware") or row.get("sc-firmware"))

    # Interface / type -- use explicit field, or inferred drive-bus-type
    drive_type = (row.get("drive-type") or row.get("disk-type")
                  or row.get("type") or row.get("drive-form-factor")
                  or row.get("interface"))
    drive_bus = row.get("drive-bus-type")   # inferred from controller

    # Use inferred-ssd flag if present (from disk-group name matching)
    if row.get("inferred-ssd") is True:
        role_id = get_or_create_inventory_role("SSD")
    elif row.get("inferred-ssd") is False:
        role_id = get_or_create_inventory_role("HDD")
    else:
        role_id = get_or_create_inventory_role("SSD") \
                  if is_ssd_storage(row) else get_or_create_inventory_role("HDD")

    # Location / slot
    location = (row.get("location") or row.get("slot")
                or row.get("durable-id"))
    health = (row.get("health") or row.get("disk-state")
              or row.get("status") or row.get("health-reason"))

    # I/O stats from disk-statistics (MSA 2040)
    extra = []
    if row.get("power-on-hours"):
        extra.append(f"PowerOnHours={row.get('power-on-hours')}")
    if row.get("data-read"):
        extra.append(f"Read={row.get('data-read')}")
    if row.get("data-written"):
        extra.append(f"Written={row.get('data-written')}")
    if row.get("iops"):
        extra.append(f"IOPS={row.get('iops')}")
    if row.get("number-of-reads"):
        extra.append(f"Reads={row.get('number-of-reads')}")
    if row.get("number-of-writes"):
        extra.append(f"Writes={row.get('number-of-writes')}")
    if row.get("number-of-media-errors-1"):
        extra.append(f"MediaErrors={row.get('number-of-media-errors-1')}")
    if row.get("number-of-nonmedia-errors-1"):
        extra.append(f"NonMediaErrors={row.get('number-of-nonmedia-errors-1')}")

    # Build a rich description with every field we found (or inferred)
    desc_parts = [f"Location={location}"]
    if model:       desc_parts.append(f"Model={model}")
    if size_str:    desc_parts.append(f"Size={size_str}")
    if drive_type:  desc_parts.append(f"Type={drive_type}")
    elif drive_bus: desc_parts.append(f"Bus={drive_bus}")
    if firmware:    desc_parts.append(f"FW={firmware}")
    if health:      desc_parts.append(f"Health={health}")
    if vendor and vendor != DEFAULT_MFR:
        desc_parts.append(f"Vendor={vendor}")
    if extra:
        desc_parts.append(" ".join(extra))

    add_item(
        name=name_storage_disk(row),
        manufacturer=vendor,
        part_number=model,
        serial=serial,
        description=" ".join(desc_parts)[:200],
        role_id=role_id,
    )
    return cap or 0

def _collect_controller_storage(row, add_item):
    serial = row.get("serial-number")
    add_item(
        name=name_storage_controller(row),
        manufacturer=DEFAULT_MFR,
        part_number=row.get("hardware-version") or row.get("model"),
        serial=serial,
        description=f"Controller={row.get('controller-id')} IP={row.get('ip-address')} "
                    f"FW={row.get('sc-firmware') or row.get('firmware-version')} Health={row.get('health')}",
        role_id=get_or_create_inventory_role("Controller"),
    )
    return 0

def _collect_psu_storage(row, add_item):
    serial = row.get("serial-number")
    add_item(
        name=name_storage_psu(row),
        manufacturer=DEFAULT_MFR,
        part_number=row.get("part-number") or row.get("model"),
        serial=serial,
        description=f"Location={row.get('location')} Health={row.get('health')} Status={row.get('status')}",
        role_id=get_or_create_inventory_role("PSU"),
    )
    return 0

def _collect_fru_storage(row, add_item):
    serial = row.get("serial-number")
    part = row.get("part-number") or row.get("fru-shortname")
    name = row.get("fru-name") or row.get("name") or "FRU"
    add_item(
        name=str(name)[:64],
        manufacturer=DEFAULT_MFR,
        part_number=part,
        serial=serial,
        description=f"Location={row.get('location')} Health={row.get('health')}",
        role_id=get_or_create_inventory_role("SAS Exp"),
    )
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# SAN switch (Brocade / HPE B-Series) — SSH CLI session + parsers
# ═══════════════════════════════════════════════════════════════════════════════
class BrocadeSwitchSession:
    """Thin SSH wrapper that runs Brocade Fabric OS CLI commands and returns
    raw text output. Works on HPE B-Series (Brocade OEM) firmware.

    Uses exec_command per call rather than an interactive shell — Fabric OS
    only allows one exec channel at a time, so calls are serialized."""

    def __init__(self, ip, port=22, timeout=20):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.client = None

    def login(self):
        self.client = paramiko.SSHClient()
        if _env_bool("SWITCH_STRICT_HOST_KEY", False):
            # Verify switch host keys against the system known_hosts
            self.client.load_system_host_keys()
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.ip, port=self.port,
            username=SWITCH_USER, password=SWITCH_PASS,
            timeout=self.timeout, allow_agent=False, look_for_keys=False,
        )
        # Verify transport is open
        if not self.client.get_transport() or not self.client.get_transport().is_active():
            raise RuntimeError(f"SSH transport not active for {self.ip}")

    def logout(self):
        try:
            if self.client:
                self.client.close()
        except Exception: pass
        self.client = None

    def run(self, command):
        """Run a CLI command via exec_command and return its stdout text.
        Brocade Fabric OS does not echo the command on exec stdout; output is
        just the command result. A trailing prompt line (if any) is stripped."""
        if not self.client:
            raise RuntimeError("SSH client not open")
        # Fabric OS rejects parallel exec channels; retry briefly if busy.
        last_err = None
        for _ in range(5):
            try:
                stdin, stdout, stderr = self.client.exec_command(
                    command, timeout=self.timeout)
                out = stdout.read().decode(errors="ignore")
                # Drain stderr to keep channel clean
                try: stderr.read()
                except Exception: pass
                return self._strip_prompt(out, command)
            except paramiko.SSHException as exc:
                last_err = exc
                time.sleep(0.5)
            except Exception as exc:
                last_err = exc
                break
        raise RuntimeError(f"exec_command '{command}' failed on {self.ip}: {last_err}")

    @staticmethod
    def _strip_prompt(text, command):
        lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            # Drop echoed command line (some firmware echoes it on exec too)
            if s == command.strip():
                continue
            # Drop trailing prompt: "admin@switch:>" or "switch>"
            if re.match(r'^[\w.-]+@[\w.-]+[>:]+\s*$', s) or re.match(r'^[\w.-]+[>:]\s*$', s):
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

# ── CLI output parsers ───────────────────────────────────────────────────────

def _parse_switchshow(text):
    """Parse `switchshow` output into key/value headers + port rows."""
    headers = {}
    ports = []
    in_ports = False
    for line in text.splitlines():
        s = line.rstrip()
        if not s: continue
        # Header: "Index Port Address Media Speed State Proto [Comment]"
        if re.match(r'^\s*Index\s+Port\s+Address\s+Media\s+Speed\s+State\s+Proto(\s+Comment)?\s*$', s, re.IGNORECASE):
            in_ports = True
            continue
        # Skip the "=" separator line that follows the header
        if in_ports and re.match(r'^=+\s*$', s):
            continue
        if in_ports:
            # Brocade port line:
            #   " 0  0  010000  id  N16  Online  FC  F-Port  51:40:2e:c0:18:1b:52:20"
            #   " 5  5  010500  id  N16  No_Light  FC  (Ports on Demand ...)"
            m = re.match(
                r'^\s*(\d+)\s+(\d+)\s+([0-9a-fA-F]+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$',
                s,
            )
            if m:
                ports.append({
                    "index":     int(m.group(1)),
                    "port":      int(m.group(2)),
                    "address":   m.group(3),
                    "media":     m.group(4),
                    "speed":     m.group(5),
                    "state":     m.group(6),
                    "proto":     m.group(7) or "",
                    "comment":   (m.group(8) or "").strip(),
                })
                continue
        # Header lines: "key: value" (before the port table)
        m = re.match(r'^([A-Za-z][\w \-/]+?):\s+(.+)$', s)
        if m and not in_ports:
            key = m.group(1).strip().lower().replace(" ", "_")
            headers[key] = m.group(2).strip()
    return headers, ports

def _parse_version(text):
    """Parse `version` / `firmwareshow` output."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^([A-Za-z][\w \-/]*?):\s+(.+)$', line.strip())
        if m:
            out[m.group(1).strip().lower().replace(" ", "_")] = m.group(2).strip()
    return out

def _parse_nsshow(text):
    """Parse `nsshow` output. Returns list of dicts per logged-in device."""
    entries = []
    cur = {}
    for line in text.splitlines():
        s = line.strip()
        if not s: continue
        # Each entry begins with a line containing "Port Id:" or "Port Name:"
        if re.match(r'^Port\s+(Id|Name|World Wide Node Name|World Wide Port Name)\s*:', s, re.IGNORECASE):
            if cur:
                entries.append(cur); cur = {}
        m = re.match(r'^([A-Za-z][\w ]*?):\s+(.+)$', s)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            cur[key] = m.group(2).strip()
    if cur:
        entries.append(cur)
    return entries

def _parse_sfpshow(text):
    """Parse `sfpshow` output. Returns list of dicts per port SFP.

    Supports two formats emitted by Fabric OS:
      1. Compact one-line-per-port (older/common firmware):
         "Port  0: id (sw) Vendor: BROCADE  Serial No: HAA...  Speed: 4,8,16_Gbps"
      2. Detailed multi-line key:value blocks (newer firmware):
         "Port  0:\\n  Identifier: ...\\n  Vendor: ..."
    """
    rows = []
    lines = text.splitlines()

    # Detect compact format: a "Port <n>: <data>" line with content after
    # the colon. Detailed-format blocks have bare "Port <n>:" headers (no
    # trailing content), so they must not be treated as compact.
    compact = any(re.match(r'^\s*Port\s+\d+\s*:\s*\S', ln) for ln in lines)

    if compact:
        for ln in lines:
            s = ln.strip()
            if not s: continue
            m = re.match(r'^\s*Port\s+(\d+)\s*:\s*(.*)$', s)
            if not m: continue
            port = int(m.group(1))
            rest = m.group(2)
            row = {"port": port}
            # Pull "Vendor: X", "Serial No: Y", "Speed: Z" out of the rest.
            # Each value extends until the next known key or end of string.
            keys = ("Vendor", "Serial No", "Speed", "Part Number", "Part No")
            for key in keys:
                mm = re.search(
                    rf'{re.escape(key)}\s*:\s*(.+?)(?=\s+(?:{"|".join(re.escape(k) for k in keys)})\s*:|$)',
                    rest,
                )
                if mm:
                    k = key.lower().replace(" ", "_")
                    row[k] = mm.group(1).strip()
            rows.append(row)
        return rows

    # Detailed multi-line format
    cur = {}
    for line in lines:
        s = line.strip()
        if not s: continue
        # New block boundary: a "Port N:" line or an "Identifier:" line
        if re.match(r'^\s*Port\s+\d+\s*:', s, re.IGNORECASE) or re.match(r'^Identifier\s*:', s, re.IGNORECASE):
            if cur:
                rows.append(cur); cur = {}
        m = re.match(r'^([A-Za-z][\w \-/]*?):\s+(.+)$', s)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            cur[key] = m.group(2).strip()
    if cur:
        rows.append(cur)
    return rows

def _wwn_normalize(wwn):
    """Normalize a WWN to colon-separated lowercase form."""
    if not wwn: return None
    s = re.sub(r'[^0-9a-fA-F]', '', str(wwn)).lower()
    if len(s) != 16: return None
    return ":".join(s[i:i+2] for i in range(0, 16, 2))

def _parse_chassisshow(text):
    """Parse `chassisshow` output for the real chassis serial + model.

    Brocade `chassisshow` returns key/value pairs like:
        Chassis PID: BES-6510
        Chassis Serial No: XXXXXXXXX
        ...
    Returns a dict with lowercased-underscore keys."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s: continue
        m = re.match(r'^([A-Za-z][\w \-/]*?):\s+(.+)$', s)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            out[key] = m.group(2).strip()
    return out

# ── probe + inventory collection ─────────────────────────────────────────────

def probe_san_switch(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, SWITCH_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        sess = BrocadeSwitchSession(ip, SWITCH_PORT)
        try:
            sess.login()
            sw = sess.run("switchshow")
            if not sw:
                if attempt < retries: time.sleep(retry_delay); continue
                return None
            headers, _ = _parse_switchshow(sw)
            ver = sess.run("version")
            ver_map = _parse_version(ver) if ver else {}
            # chassisshow gives the real supplier serial + model (PID)
            chs = sess.run("chassisshow")
            chs_map = _parse_chassisshow(chs) if chs else {}
            model = (chs_map.get("supplier_part_num")
                      or chs_map.get("chassis_pid")
                      or headers.get("switchtype") or headers.get("switch_type")
                      or headers.get("model") or headers.get("product"))
            serial = (chs_map.get("serial_num")
                       or chs_map.get("factory_serial_num")
                       or chs_map.get("chassis_serial_number")
                       or chs_map.get("chassis_serial_no")
                       or chs_map.get("serial_number")
                       or headers.get("switchwwn") or headers.get("switch_wwn"))
            wwn = _wwn_normalize(headers.get("switchwwn") or headers.get("switch_wwn"))
            name = headers.get("switch_name") or headers.get("switchname") or f"san-{ip.replace('.', '-')}"
            fw = (ver_map.get("fabric_os") or ver_map.get("kernel") or
                  ver_map.get("firmware") or ver_map.get("version"))
            return {
                "ip":           ip,
                "host":         f"{ip}:{SWITCH_PORT}",
                "serial":       serial,
                "model":        model,
                "hostname":     name.strip(),
                "manufacturer": "Brocade",
                "wwn":          wwn,
                "firmware":     fw,
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        finally:
            try: sess.logout()
            except Exception: pass
    return None

def san_collect_inventory(ip):
    """Full inventory pull for a SAN switch: identity, ports, nameserver, SFPs."""
    sess = BrocadeSwitchSession(ip, SWITCH_PORT)
    sess.login()
    try:
        sw_text = sess.run("switchshow")
        headers, ports = _parse_switchshow(sw_text)
        log("INFO", f"  switchshow: {len(sw_text)} bytes, {len(headers)} headers, {len(ports)} ports")
        ver_map = _parse_version(sess.run("version") or "")
        # chassisshow gives the real supplier serial + model (PID)
        try:
            chs_text = sess.run("chassisshow") or ""
            chs_map = _parse_chassisshow(chs_text) if chs_text else {}
            log("INFO", f"  chassisshow: {len(chs_text)} bytes, {len(chs_map)} fields")
        except Exception as exc:
            chs_map = {}
            log("WARN", f"  chassisshow failed: {exc}")
        # nameserver: combine nsshow + nscamshow to catch all logged-in devices
        ns_entries = []
        for cmd in ("nsshow", "nscamshow"):
            try:
                ns_text = sess.run(cmd) or ""
                ns_count = len(_parse_nsshow(ns_text))
                log("INFO", f"  {cmd}: {len(ns_text)} bytes, {ns_count} entries")
                ns_entries.extend(_parse_nsshow(ns_text))
            except Exception as exc:
                log("WARN", f"  {cmd} failed: {exc}")
        try:
            sfp_text = sess.run("sfpshow") or ""
            sfp_rows = _parse_sfpshow(sfp_text)
            log("INFO", f"  sfpshow: {len(sfp_text)} bytes, {len(sfp_rows)} SFPs")
        except Exception as exc:
            sfp_rows = []
            log("WARN", f"  sfpshow failed: {exc}")

        # Prefer chassisshow supplier (OEM) fields for real serial + model; fall back to switchshow
        chs_serial = (chs_map.get("serial_num")
                      or chs_map.get("factory_serial_num")
                      or chs_map.get("chassis_serial_number")
                      or chs_map.get("chassis_serial_no")
                      or chs_map.get("serial_number"))
        chs_model = (chs_map.get("supplier_part_num")
                     or chs_map.get("chassis_pid")
                     or chs_map.get("pid")
                     or chs_map.get("chassis_product_id"))
        sw_model_raw = (headers.get("switchtype") or headers.get("switch_type")
                        or headers.get("model"))
        summary = {
            "serial":    (chs_serial
                          or headers.get("switchwwn") or headers.get("switch_wwn")
                          or headers.get("serial_number")),
            "wwn":       _wwn_normalize(headers.get("switchwwn") or headers.get("switch_wwn")),
            "model":     (chs_model
                          or normalize_model(sw_model_raw, SWITCH_MODEL_MAP)
                          or sw_model_raw),
            "firmware":  (ver_map.get("fabric_os") or ver_map.get("kernel") or
                          ver_map.get("firmware") or ver_map.get("version")),
            "hostname":  (headers.get("switch_name") or headers.get("switchname") or "").strip(),
            "port_count": len(ports),
        }

        # Build inventory items: SFPs and FC port modules
        inventory = {}
        add_item = _make_add_item(inventory)

        # Map SFP rows to ports by index when possible (sfpshow is ordered)
        for idx, sfp in enumerate(sfp_rows):
            sfp_serial = (sfp.get("vendor_serial_number") or sfp.get("serial_no")
                          or sfp.get("serial_number"))
            if not _invalid_serial(sfp_serial):
                port_num = sfp.get("port", idx)
                add_item(
                    name=f"SFP Port {port_num}",
                    manufacturer=(sfp.get("vendor_name") or sfp.get("vendor")
                                  or "Brocade"),
                    part_number=(sfp.get("vendor_part_number")
                                 or sfp.get("part_number") or sfp.get("part_no")),
                    serial=sfp_serial,
                    description=(f"Port={port_num} Type={sfp.get('identifier') or 'SFP'} "
                                 f"Speed={sfp.get('speed') or sfp.get('speed_capability')} "
                                 f"Temp={sfp.get('temperature')} "
                                 f"VendorPN={sfp.get('vendor_part_number') or sfp.get('part_no')}"),
                    role_id=get_or_create_inventory_role("SFP", "4caf50"),
                )

        return {
            "summary":    summary,
            "ports":      ports,
            "nameserver": ns_entries,
            "sfp":        sfp_rows,
            "inventory":  inventory,
        }
    finally:
        sess.logout()

# ── NetBox CRUD for SAN switches ─────────────────────────────────────────────

def ensure_san_switch_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Brocade")
    role_id = get_or_create_role(SWITCH_ROLE, "f44336")
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, SWITCH_MODEL_MAP)
    name = _device_name(probe, prefix="san")
    api = get_netbox()
    dev = find_device(serial, role_name=SWITCH_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found san switch by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "san_switch_ip":      probe["ip"],
            "san_switch_enabled": True,
            "san_switch_wwn":     probe.get("wwn"),
            "san_switch_firmware": probe.get("firmware"),
            "san_switch_model":   probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  SAN switch updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  SAN switch created: {name} (id={new.id})")
    return new.id

def mark_san_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"san_switch_enabled": False},
        }])
        log("WARN", f"  SAN switch marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark SAN switch offline {dev_name}: {e}")

# Brocade speed token -> NetBox interface type choice string.
# Matched on the numeric part so "N16"/"16G"/"16" all map the same way.
_FC_SPEED_TYPES = {
    1:   "1gfc-sfp",
    2:   "2gfc-sfp",
    4:   "4gfc-sfp",
    8:   "8gfc-sfpp",
    16:  "16gfc-sfpp",
    32:  "32gfc-sfp28",
    64:  "64gfc-qsfpp",
    128: "128gfc-qsfp28",
}

def _fc_interface_type(speed):
    """Map a Brocade port speed string to a NetBox interface type choice.

    NetBox interface `type` is a choice string (not an ID), e.g.
    '8gfc-sfpp', '16gfc-sfpp', '32gfc-sfp28', 'other'. Returns 'other'
    if the speed is unknown or the port is offline."""
    s = (speed or "").lower().strip()
    m = re.match(r'^n?(\d+)', s)   # "N16" -> 16, "16G" -> 16, "16" -> 16
    if not m:
        return "other"
    return _FC_SPEED_TYPES.get(int(m.group(1)), "other")

def sync_san_interfaces(dev_id, ports, nameserver):
    """Create/update NetBox interfaces for each FC port on the switch.
    Connected device WWN (from nameserver) is stored in interface description."""
    api = get_netbox()
    existing = {}
    for iface in list(api.dcim.interfaces.filter(device_id=dev_id)):
        existing[str(iface.name)] = iface

    # Map port index -> logged-in WWNs (from nameserver entries that include port id)
    port_wwns = {}
    for ns in nameserver:
        # Port Id typically like "010c00" -> first 2 hex digits = switch port index (octant)
        pid = (ns.get("port_id") or "").lower()
        if len(pid) >= 2:
            try: idx = int(pid[:2], 16)
            except Exception: continue
            wwn = _wwn_normalize(ns.get("port_world_wide_name") or ns.get("world_wide_port_name"))
            if wwn: port_wwns.setdefault(idx, []).append(wwn)

    seen = set()
    for p in ports:
        name = f"FC {p['port']}"
        seen.add(name)
        desc_parts = [f"speed={p.get('speed')}", f"state={p.get('state')}"]
        wwns = port_wwns.get(p["index"]) or []
        if wwns:
            desc_parts.append("WWNs=" + ",".join(wwns))
        elif p.get("comment"):
            desc_parts.append(p["comment"])
        payload = {
            "device":     dev_id,
            "name":       name,
            "type":       _fc_interface_type(p.get("speed")),
            "enabled":    p.get("state", "").lower() == "online",
            "description": " | ".join(desc_parts)[:200],
            "mgmt_only":  False,
        }
        if name in existing:
            api.dcim.interfaces.update([{"id": existing[name].id, **payload}])
        else:
            try:
                api.dcim.interfaces.create(payload)
            except Exception as e:
                log("WARN", f"  Could not create interface {name}: {e}")

    # Remove interfaces that no longer exist on the switch
    for name, iface in existing.items():
        if name not in seen:
            try: iface.delete()
            except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════════
# NetBox inventory sync (shared)
# ═══════════════════════════════════════════════════════════════════════════════
def sync_inventory(dev_id, new_inventory):
    api = get_netbox()
    # Single fetch — group existing items by serial
    by_serial = {}
    for item in api.dcim.inventory_items.filter(device_id=dev_id):
        s = str(item.serial or "").strip()
        if s: by_serial.setdefault(s, []).append(item)

    # Delete duplicate entries for the same serial outright; the canonical
    # item is recreated below from the freshly collected inventory.
    for s, items in by_serial.items():
        if len(items) > 1:
            for item in items: item.delete()
            by_serial[s] = []

    # Delete items the device no longer reports
    new_serials = set(new_inventory.keys())
    for s, items in by_serial.items():
        if items and s not in new_serials:
            for item in items: item.delete()
            by_serial[s] = []

    # What remains are live single items whose serial is still reported
    live = {s: items[0] for s, items in by_serial.items() if items}

    for serial, item in new_inventory.items():
        mfr_id = get_or_create_manufacturer(item.get("manufacturer"))
        payload = {
            "device":      dev_id,
            "name":        item["name"],
            "manufacturer": mfr_id,
            "part_id":     item.get("part_number") or "",
            "serial":      serial,
            "description": item.get("description") or "",
            **({"role": item["role"]} if item.get("role") else {}),
        }
        if serial in live:
            api.dcim.inventory_items.update([{"id": live[serial].id, **payload}])
        else:
            api.dcim.inventory_items.create(payload)


# ═══════════════════════════════════════════════════════════════════════════════
# main sync job
# ═══════════════════════════════════════════════════════════════════════════════
def run_sync():
    log("INFO", "=" * 60)
    log("INFO", "Unified sync started (servers + storage + SAN switches)")
    log("INFO", "=" * 60)

    found = scan_all()
    api = get_netbox()

    # ── Process servers ───────────────────────────────────────────────────────
    live_server_ips = {h["ip"] for h in found["servers"]}
    for probe in found["servers"]:
        ip = probe["ip"]
        host = probe["host"]
        log("INFO", f"Processing SERVER {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_server_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_server_device failed for {ip}: {e}"); continue

        try:
            data = rf_collect_inventory(host)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  inventory collection failed for {ip}: {e}"); continue

        s   = data["summary"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "bmc_ip":                 ip,
                    "redfish_enabled":        True,
                    "redfish_model":          s.get("model"),
                    "redfish_power_state":    s.get("power_state"),
                    "redfish_bios_version":   s.get("bios_version"),
                    "redfish_cpu_model":      s.get("cpu_model"),
                    "redfish_cpu_sockets":    s.get("cpu_sockets"),
                    "redfish_cpu_cores":      s.get("cpu_cores"),
                    "redfish_cpu_threads":    s.get("cpu_threads"),
                    "redfish_ram_gib":        s.get("ram_gib"),
                    "redfish_disk_total_gib": s.get("disk_total_gib"),
                },
            }
            if s.get("serial"): payload["serial"] = s["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  server update failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Server {ip} — {len(inv)} items synced")
        except Exception as e:
            log("ERROR", f"  inventory sync failed for {ip}: {e}")

    # ── Process storage ──────────────────────────────────────────────────────
    live_storage_ips = {h["ip"] for h in found["storage"]}
    for probe in found["storage"]:
        ip = probe["ip"]
        log("INFO", f"Processing STORAGE {ip}  ({probe.get('model')} / {probe.get('serial')})")

        try:
            dev_id = ensure_storage_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_storage_device failed for {ip}: {e}"); continue

        try:
            data = storage_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "storage_ip":                 ip,
                    "storage_enabled":            True,
                    "storage_health":             summary.get("health") or probe.get("health"),
                    "storage_firmware":           summary.get("firmware") or probe.get("firmware"),
                    "storage_model":              summary.get("model") or probe.get("model"),
                    "storage_disk_count":         summary.get("disk_count"),
                    "storage_total_capacity_gib": summary.get("disk_total_gib"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  storage update failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] Storage {ip} — {len(inv)} items synced")
        except Exception as e:
            log("ERROR", f"  inventory sync failed for {ip}: {e}")

    # ── Process SAN switches ──────────────────────────────────────────────────
    live_san_ips = {h["ip"] for h in found["san_switches"]}
    for probe in found["san_switches"]:
        ip = probe["ip"]
        log("INFO", f"Processing SAN SWITCH {ip}  ({probe.get('model')} / wwn={probe.get('wwn')})")

        try:
            dev_id = ensure_san_switch_device(probe)
        except Exception as e:
            log("ERROR", f"  ensure_san_switch_device failed for {ip}: {e}"); continue

        try:
            data = san_collect_inventory(ip)
        except KeyboardInterrupt: raise
        except Exception as e:
            log("ERROR", f"  SAN inventory collection failed for {ip}: {e}"); continue

        summary = data["summary"]
        ports = data["ports"]
        nameserver = data["nameserver"]
        inv = data["inventory"]

        try:
            payload = {
                "id": dev_id,
                "status": "active",
                "custom_fields": {
                    "san_switch_ip":        ip,
                    "san_switch_enabled":   True,
                    "san_switch_wwn":       summary.get("wwn") or probe.get("wwn"),
                    "san_switch_firmware":  summary.get("firmware") or probe.get("firmware"),
                    "san_switch_model":     summary.get("model") or probe.get("model"),
                    "san_switch_port_count": summary.get("port_count"),
                },
            }
            if summary.get("serial"): payload["serial"] = summary["serial"]
            api.dcim.devices.update([payload])
        except Exception as e:
            log("ERROR", f"  SAN switch update failed for {ip}: {e}")

        try:
            sync_san_interfaces(dev_id, ports, nameserver)
            log("INFO", f"  [OK] SAN {ip} — {len(ports)} ports, {len(nameserver)} nameserver entries")
        except Exception as e:
            log("ERROR", f"  SAN interface sync failed for {ip}: {e}")

        try:
            sync_inventory(dev_id, inv)
            log("INFO", f"  [OK] SAN {ip} — {len(inv)} inventory items synced")
        except Exception as e:
            log("ERROR", f"  SAN inventory sync failed for {ip}: {e}")

    # ── Mark unreachable devices offline ─────────────────────────────────────
    # A device must be missing from OFFLINE_THRESHOLD consecutive scans before
    # being marked offline. This prevents transient iLO slowness under load
    # from causing false offline markings.
    log("INFO", "Checking for unreachable servers (Redfish) ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_redfish_enabled=True)):
            bmc_ip = (dev.custom_fields or {}).get("bmc_ip")
            if not bmc_ip: continue
            ip = bmc_ip.split("/")[0].strip()
            _check_offline(ip, live_server_ips, dev.id, dev.name,
                           mark_server_offline, "Server")
    except Exception as e:
        log("ERROR", f"Server offline check failed: {e}")

    log("INFO", "Checking for unreachable storage ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_storage_enabled=True)):
            storage_ip = (dev.custom_fields or {}).get("storage_ip")
            if not storage_ip: continue
            ip = str(storage_ip).split("/")[0].strip()
            _check_offline(ip, live_storage_ips, dev.id, dev.name,
                           mark_storage_offline, "Storage")
    except Exception as e:
        log("ERROR", f"Storage offline check failed: {e}")

    log("INFO", "Checking for unreachable SAN switches ...")
    try:
        for dev in list(api.dcim.devices.filter(cf_san_switch_enabled=True)):
            san_ip = (dev.custom_fields or {}).get("san_switch_ip")
            if not san_ip: continue
            ip = str(san_ip).split("/")[0].strip()
            _check_offline(ip, live_san_ips, dev.id, dev.name,
                           mark_san_offline, "SAN switch")
    except Exception as e:
        log("ERROR", f"SAN switch offline check failed: {e}")

    log("INFO", "Unified sync complete")
    log("INFO", "=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# scheduler
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        _validate_config()
        schedule.every().day.at("00:00").do(run_sync)
        schedule.every().day.at("12:00").do(run_sync)
        log("INFO", "Scheduler started — runs at 00:00 and 12:00 daily.")
        log("INFO", "Running initial unified sync now ...")
        run_sync()
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
