"""FortiGate firewalls: REST API session (identity/interfaces/VLANs) plus
SSH extras (LLDP neighbors, SFP transceivers)."""
import re
import time

import requests
from netmiko import ConnectHandler

from netbox_sync import netbox
from netbox_sync.config import (FORTIGATE_USER, FORTIGATE_PASS, FORTIGATE_PORT,
                                FORTIGATE_SSH_PORT, FORTIGATE_TOKENS, log)
from netbox_sync.models import FORTIGATE_MODEL_MAP
from netbox_sync.utils import (normalize_model, _invalid_serial,
                               _make_add_item, is_port_open)

# ── REST API session + mappers ───────────────────────────────────────────────

class FortiGateSession:
    def __init__(self, ip, port, token, timeout=30):
        self.base = f"https://{ip}:{port}"
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({"Authorization": f"Bearer {token}"})
        self.timeout = timeout

    def get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

def _fg_status(data):
    """Map /monitor/system/status JSON to identity fields. FortiOS puts the
    serial at TOP level and splits the model into model_name/model_number
    (7.2.x); older builds nest serial_number inside results."""
    results = data.get("results") or data
    model_name   = (results.get("model_name") or "").strip()
    model_number = (results.get("model_number") or "").strip()
    if model_name and model_number:
        model = f"{model_name} {model_number}"       # "FortiGate 1800F"
    else:
        model = (results.get("model") or model_number or model_name or None)
    return {
        "hostname": results.get("hostname"),
        "serial":   data.get("serial") or results.get("serial_number"),
        "model":    model,
        "version":  data.get("version") or results.get("version"),
    }

def _fg_speed(m):
    s = str(m.get("speed") or "")
    digits = re.match(r'^(\d+)', s)
    return int(digits.group(1)) if digits else None

def _fg_interfaces(monitor_data, cmdb_data):
    """Merge /monitor/system/interface (link/speed) with /cmdb config.
    Monitor only reports base interfaces (no VLAN subinterfaces on FortiOS
    7.x), so cmdb vlan rows absent from monitor are unioned in."""
    mon = monitor_data.get("results") or {}
    cfg = cmdb_data.get("results") or []
    cfg_by_name = {c.get("name"): c for c in cfg if isinstance(c, dict)}
    ports = []
    for name, m in mon.items():
        if not isinstance(m, dict): continue
        c = cfg_by_name.get(name, {})
        ports.append({
            "name": name,
            "link": bool(m.get("link")),
            "speed_mbps": _fg_speed(m),
            "type": c.get("type") or "",
            "ip": c.get("ip") or "",
            "vlanid": c.get("vlanid"),
            "parent": c.get("interface") or "",
            "alias": c.get("alias") or "",
        })
    for c in cfg:
        if not isinstance(c, dict): continue
        if c.get("type") != "vlan" or c.get("name") in mon:
            continue
        ports.append({
            "name": c.get("name"),
            "link": True,   # configured subinterface; monitor has no stats
            "speed_mbps": None,
            "type": "vlan",
            "ip": c.get("ip") or "",
            "vlanid": c.get("vlanid"),
            "parent": c.get("interface") or "",
            "alias": c.get("alias") or "",
        })
    return ports

def _fg_vlans(cmdb_data):
    out = []
    for c in (cmdb_data.get("results") or []):
        if not isinstance(c, dict): continue
        if c.get("type") == "vlan" and c.get("vlanid") is not None:
            out.append({"vid": int(c["vlanid"]),
                        "name": c.get("name") or f"VLAN{int(c['vlanid']):04d}",
                        "status": "active"})
    return out

def _fg_interface_type(speed_mbps):
    return {100: "100base-tx", 1000: "1000base-t",
            10000: "10gbase-t", 25000: "25gbase-x-sfp28",
            40000: "40gbase-x-qsfpp"}.get(speed_mbps, "other")

# ── SSH extras (LLDP + transceivers) ─────────────────────────────────────────

# FortiOS prints command failures INLINE (netmiko sees no exception) —
# detect them instead of silently parsing an error page to zero rows.
_FG_CMD_FAIL = re.compile(r'(Unknown action|Command fail|command parse error)',
                          re.IGNORECASE)

def _ssh_run_or_none(sess, command, label):
    """Run a FortiOS command; return None (with an informative WARN) when the
    command errors or is rejected/unsupported on this device."""
    try:
        out = sess.run(command)
    except Exception as exc:
        log("WARN", f"  {label} failed: {exc}")
        return None
    if _FG_CMD_FAIL.search(out or ""):
        log("WARN", f"  {label} not available on this device (command rejected)")
        return None
    return out

class FortiGateSSHSession:
    def __init__(self, ip, timeout=20):
        self.ip = ip
        self.timeout = timeout
        self.conn = None

    def login(self):
        self.conn = ConnectHandler(
            device_type="fortinet", host=self.ip, port=FORTIGATE_SSH_PORT,
            username=FORTIGATE_USER, password=FORTIGATE_PASS,
            conn_timeout=self.timeout, auth_timeout=self.timeout,
            banner_timeout=self.timeout)

    def run(self, command):
        if not self.conn:
            raise RuntimeError("SSH session not open")
        return self.conn.send_command(command, read_timeout=self.timeout)

    def logout(self):
        try:
            if self.conn:
                self.conn.disconnect()
        except Exception: pass
        self.conn = None

def _parse_lldp_summary(text):
    """Parse `diagnose lldp neighbor-summary` into CDP-shaped neighbors."""
    entries = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("-") or re.match(r'^Port\s', s):
            continue
        m = re.match(r'^(\S+)\s+([0-9a-fA-F:]{17})\s+(.+?)\s+([A-Z,]+)\s+(\d+)\s+(\S+)$', s)
        if not m: continue
        entries.append({"device_id": m.group(3), "platform": "",
                        "local_intf": m.group(1), "remote_intf": m.group(6),
                        "ip": None})
    return entries

def _parse_ifconfig_a(text):
    """Parse `fnsysctl ifconfig -a` blocks: interface name -> MAC
    (lowercase colon form)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^(.+?)\tLink encap:Ethernet\s+HWaddr\s+([0-9A-Fa-f:]{17})', line)
        if m:
            out[m.group(1).strip()] = m.group(2).lower()
    return out

def _parse_transceivers(text):
    """Parse `diagnose sys transceiver list`: per-port vendor/part/serial."""
    rows = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^Port\s+(\d+)\s*:', s)
        if m:
            if cur: rows.append(cur)
            cur = {"port": int(m.group(1))}
            continue
        if cur is None: continue
        m = re.match(r'^(Vendor|Part Number|Serial Number)\s*:\s*(.+)$', s)
        if m:
            key = m.group(1).lower().replace(" ", "_")
            cur[key] = m.group(2).strip()
    if cur: rows.append(cur)
    return rows

# ── probe + collect ──────────────────────────────────────────────────────────

def probe_fortigate(ip, retries=2, retry_delay=3):
    entry = FORTIGATE_TOKENS.get(ip)
    if not entry:
        log("DEBUG", f"  no FortiGate API token for {ip} — skipping")
        return None
    port, token = entry
    for attempt in range(1, retries + 1):
        # One quick port check is enough for dead IPs (same reasoning as Cisco)
        if not is_port_open(ip, port, timeout=3, retries=1):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        try:
            status = _fg_status(
                FortiGateSession(ip, port, token).get("/api/v2/monitor/system/status"))
            if not (status.get("serial") or status.get("model")):
                raise RuntimeError("status yielded no serial/model")
            return {
                "ip": ip, "host": f"{ip}:{port}",
                "serial": status.get("serial"),
                "model": (normalize_model(status.get("model"), FORTIGATE_MODEL_MAP)
                          or status.get("model")),
                "hostname": (status.get("hostname")
                             or f"fortigate-{ip.replace('.', '-')}"),
                "manufacturer": "Fortinet",
                "firmware": status.get("version"),
            }
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
    return None

def fortigate_collect(ip):
    entry = FORTIGATE_TOKENS.get(ip)
    if not entry:
        raise RuntimeError(f"no FortiGate API token for {ip}")
    port, token = entry
    fg = FortiGateSession(ip, port, token)
    status = _fg_status(fg.get("/api/v2/monitor/system/status"))
    mon = fg.get("/api/v2/monitor/system/interface")
    cmdb = fg.get("/api/v2/cmdb/system/interface?vdom=root")
    ports = _fg_interfaces(mon, cmdb)
    vlans = _fg_vlans(cmdb)
    log("INFO", f"  fortigate api: {len(ports)} interfaces, {len(vlans)} vlans")

    neighbors = []
    inventory = {}
    sess = FortiGateSSHSession(ip)
    try:
        sess.login()
        lldp_out = _ssh_run_or_none(sess, "diagnose lldp neighbor-summary", "lldp")
        if lldp_out is not None:
            neighbors = _parse_lldp_summary(lldp_out)
            log("INFO", f"  lldp neighbors: {len(neighbors)}")
        sfp_out = _ssh_run_or_none(sess, "diagnose sys transceiver list", "transceivers")
        if sfp_out is not None:
            add = _make_add_item(inventory)
            for row in _parse_transceivers(sfp_out):
                serial = row.get("serial_number")
                if _invalid_serial(serial): continue
                add(name=f"SFP Port {row.get('port')}",
                    manufacturer=row.get("vendor") or "Unknown",
                    part_number=row.get("part_number"), serial=serial,
                    description=f"Port={row.get('port')}",
                    role_id=netbox.get_or_create_inventory_role("SFP", "4caf50"))
            log("INFO", f"  transceivers: {len(inventory)}")
        ifc_out = _ssh_run_or_none(sess, "fnsysctl ifconfig -a", "ifconfig")
        if_macs = _parse_ifconfig_a(ifc_out) if ifc_out is not None else {}
    except Exception as exc:
        log("WARN", f"  fortigate ssh failed for {ip}: {exc}")
    finally:
        try: sess.logout()
        except Exception: pass

    summary = {
        "serial": status.get("serial"),
        "model": (normalize_model(status.get("model"), FORTIGATE_MODEL_MAP)
                  or status.get("model")),
        "firmware": status.get("version"),
        "hostname": (status.get("hostname") or "").strip(),
        "port_count": len(ports),
    }
    vlan_macs = {}
    for v in vlans:
        mac = if_macs.get(v["name"])
        if mac:
            vlan_macs[v["vid"]] = mac

    return {"summary": summary, "ports": ports, "vlans": vlans,
            "neighbors": neighbors, "inventory": inventory,
            "vlan_macs": vlan_macs}

# ── interfaces ───────────────────────────────────────────────────────────────

def sync_fortigate_interfaces(dev_id, ports, vid_map):
    """Bulk create/update FortiGate interfaces: physical ports typed by
    speed, VLAN subinterfaces as virtual with untagged_vlan."""
    api = netbox.get_netbox()
    existing = {str(i.name): i
                for i in api.dcim.interfaces.filter(device_id=dev_id)}
    seen = set()
    updates, creates = [], []
    for p in ports:
        name = p["name"]
        seen.add(name)
        if p.get("type") == "vlan" and p.get("vlanid") is not None:
            payload = {"device": dev_id, "name": name, "type": "virtual",
                       "enabled": p.get("link", False),
                       "description": f"vlanid={p['vlanid']} ip={p.get('ip')}"[:200],
                       "mgmt_only": False}
            if p["vlanid"] in vid_map:
                payload["mode"] = "tagged"
                payload["untagged_vlan"] = vid_map[p["vlanid"]]
        else:
            payload = {"device": dev_id, "name": name,
                       "type": _fg_interface_type(p.get("speed_mbps")),
                       "enabled": bool(p.get("link")),
                       "description": f"type={p.get('type')} ip={p.get('ip')}"[:200],
                       "mgmt_only": False}
        if p.get("alias"):
            payload["label"] = str(p["alias"])[:64]
        if name in existing:
            updates.append({"id": existing[name].id, **payload})
        else:
            creates.append(payload)
    if updates:
        api.dcim.interfaces.update(updates)
    if creates:
        try:
            api.dcim.interfaces.create(creates)
        except Exception as e:
            log("WARN", f"  Could not create interfaces: {e}")
    for name, iface in existing.items():
        if name not in seen:
            if getattr(iface, "mgmt_only", False):
                continue   # never delete management interfaces
            try: iface.delete()
            except Exception: pass
