"""NetBox API layer: connection, get-or-create helpers, device ensure /
mark-offline, and serial-keyed inventory reconciliation.

Inventory item roles are resolved by NAME via get_or_create_inventory_role().
Role IDs are DB-sequence-dependent and NOT portable between NetBox
instances, so nothing here may hardcode them.
"""
import pynetbox

from netbox_sync.config import (NETBOX_URL, NETBOX_TOKEN, _env_bool,
                                SERVER_ROLE, STORAGE_ROLE, SWITCH_ROLE,
                                CISCO_ROLE,
                                DEFAULT_MFR, OFFLINE_THRESHOLD, log)
from netbox_sync.models import (SERVER_MODEL_MAP, STORAGE_MODEL_MAP,
                                SWITCH_MODEL_MAP, CISCO_MODEL_MAP)
from netbox_sync.utils import (slugify, normalize_model, resolve_site_from_name,
                               _invalid_serial)

nb = None

def get_netbox():
    global nb
    if nb is not None:
        return nb
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    # TLS verification for NetBox is opt-in (NETBOX_VERIFY_TLS=true) since
    # many internal NetBox installs use self-signed certs.
    nb.http_session.verify = _env_bool("NETBOX_VERIFY_TLS", False)
    return nb

# ── CRUD helpers ─────────────────────────────────────────────────────────────
# Resolution caches: a sync run is short-lived and NetBox names/slugs are
# unique, so caching id lookups for the process lifetime is safe and avoids
# re-querying the same manufacturer/role/site/type once per device or item.
_MANUFACTURER_CACHE = {}
_ROLE_CACHE = {}
_SITE_CACHE = {}
_DEVICE_TYPE_CACHE = {}

def _get_or_create(endpoint, lookup, create):
    obj = endpoint.get(**lookup)
    if obj: return obj.id
    return endpoint.create(create).id

def get_or_create_manufacturer(name):
    if not name: return None
    name = name.strip()
    key = name.lower()
    if key in _MANUFACTURER_CACHE:
        return _MANUFACTURER_CACHE[key]
    mfr_id = _resolve_manufacturer(name)
    if mfr_id is not None:
        _MANUFACTURER_CACHE[key] = mfr_id
    return mfr_id

def _resolve_manufacturer(name):
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
    key = (m.lower(), mfr_id)
    if key in _DEVICE_TYPE_CACHE:
        return _DEVICE_TYPE_CACHE[key]
    dt_id = _get_or_create(get_netbox().dcim.device_types, {"model": m},
                           {"model": m, "slug": slugify(m), "manufacturer": mfr_id})
    _DEVICE_TYPE_CACHE[key] = dt_id
    return dt_id

def get_or_create_role(name, color="9e9e9e"):
    key = name.lower()
    if key in _ROLE_CACHE:
        return _ROLE_CACHE[key]
    api = get_netbox()
    r = api.dcim.device_roles.get(name=name)
    if not r:
        r = api.dcim.device_roles.get(slug=slugify(name))
    if not r:
        r = api.dcim.device_roles.create(
            {"name": name, "slug": slugify(name), "color": color})
    _ROLE_CACHE[key] = r.id
    return r.id

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
    key = name.lower()
    if key in _SITE_CACHE:
        return _SITE_CACHE[key]
    site_id = _get_or_create(get_netbox().dcim.sites, {"name": name},
                             {"name": name, "slug": slugify(name), "status": "active"})
    _SITE_CACHE[key] = site_id
    return site_id

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

# ── device ensure / mark offline ─────────────────────────────────────────────
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

def ensure_cisco_device(probe):
    serial = (probe.get("serial") or "").strip()
    mfr_id = get_or_create_manufacturer(probe.get("manufacturer") or "Cisco")
    role_id = get_or_create_role(CISCO_ROLE, "009688")
    site_name = resolve_site_from_name(probe.get("hostname") or "")
    site_id = get_or_create_site(site_name)
    dtype_id = get_or_create_device_type(probe.get("model"), mfr_id, CISCO_MODEL_MAP)
    name = _device_name(probe, prefix="cisco")
    api = get_netbox()
    dev = find_device(serial, role_name=CISCO_ROLE)
    if dev is None:
        cands = list(api.dcim.devices.filter(name=name, site_id=site_id, role_id=role_id))
        dev = cands[0] if cands else None
        if dev: log("INFO", f"  Found cisco switch by name+site: {name} (id={dev.id})")
    payload = {
        "name": name, "status": "active", "site": site_id,
        "device_type": dtype_id, "role": role_id,
        "custom_fields": {
            "cisco_ip":       probe["ip"],
            "cisco_enabled":  True,
            "cisco_firmware": probe.get("firmware"),
            "cisco_model":    probe.get("model"),
        },
        **({"serial": serial} if not _invalid_serial(serial) else {}),
    }
    if dev:
        api.dcim.devices.update([{"id": dev.id, **payload}])
        log("INFO", f"  Cisco switch updated: {name} (id={dev.id})")
        return dev.id
    new = api.dcim.devices.create(payload)
    log("INFO", f"  Cisco switch created: {name} (id={new.id})")
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

def mark_san_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"san_switch_enabled": False},
        }])
        log("WARN", f"  SAN switch marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark SAN switch offline {dev_name}: {e}")

def mark_cisco_offline(dev_id, dev_name):
    try:
        get_netbox().dcim.devices.update([{
            "id": dev_id, "status": "offline",
            "custom_fields": {"cisco_enabled": False},
        }])
        log("WARN", f"  Cisco switch marked offline: {dev_name} (id={dev_id})")
    except Exception as e:
        log("ERROR", f"  Could not mark Cisco switch offline {dev_name}: {e}")

# ── Consecutive-failure tracking (prevents flapping) ─────────────────────────
# A device must fail to appear in the scan for OFFLINE_THRESHOLD consecutive
# runs before being marked offline. The counter persists across scheduled runs
# in process memory and resets to 0 the moment the device is seen again.
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

# ── inventory sync (shared) ──────────────────────────────────────────────────
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
