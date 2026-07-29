"""Pure helpers: naming, capacity math, serial handling, Redfish field
extraction and IP/network utilities. No NetBox or hardware access here."""
import ipaddress
import re
import socket
import time

from netbox_sync.config import SITE_KEYWORD_MAP, SITE_UNKNOWN, SITE_IP_MAP

# ── generic helpers ──────────────────────────────────────────────────────────
def slugify(s):
    return re.sub(r'[^a-z0-9-]', '-', s.lower().strip())[:50].strip('-')

def normalize_model(model, model_map):
    if not model: return None
    return model_map.get(model.strip().lower(), model.strip())

def resolve_site(hostname, ip):
    """Site resolution: IP-range map first (longest-prefix-match — the list
    is pre-sorted most-specific-first), then hostname keyword, then
    SITE_UNKNOWN."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except (ValueError, AttributeError):
        addr = None
    if addr is not None:
        for net, site in SITE_IP_MAP:
            try:
                if addr in net:
                    return site
            except TypeError:
                continue   # mixed IPv4/IPv6 entry — not a match
    name_lower = (hostname or "").lower()
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

# ── smart item naming (server) ───────────────────────────────────────────────
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

# ── smart item naming (storage) ──────────────────────────────────────────────
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

# ── IP scanning ──────────────────────────────────────────────────────────────
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
