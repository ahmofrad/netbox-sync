"""Cisco Catalyst (IOS / IOS-XE) switches: netmiko SSH session, CLI output
parsers, probing, inventory collection, interface sync and CDP/LLDP cable
reconciliation."""
import re
import time

from netmiko import ConnectHandler

from netbox_sync import netbox
from netbox_sync.config import (CISCO_USER, CISCO_PASS, CISCO_PORT, log)
from netbox_sync.models import CISCO_MODEL_MAP
from netbox_sync.utils import (normalize_model, _invalid_serial,
                               _make_add_item, is_port_open)

# ── CLI output parsers ───────────────────────────────────────────────────────

def _parse_show_version(text):
    """Parse `show version` for both classic IOS and IOS-XE dialects."""
    out = {"hostname": None, "model": None, "serial": None, "ios_version": None}
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^(\S+)\s+uptime is\b', s, re.IGNORECASE)
        if m and not out["hostname"]:
            out["hostname"] = m.group(1)
        m = re.search(r'Version\s+(\S+?)(?:,|\s+RELEASE)', s)
        if m and not out["ios_version"]:
            out["ios_version"] = m.group(1)
        m = re.match(r'^cisco\s+(\S+)\s+\(', s, re.IGNORECASE)
        if m and not out["model"]:
            out["model"] = m.group(1)
        m = re.match(r'^Model\s+(?:number|Number)\s*:\s*(\S+)', s)
        if m and not out["model"]:
            out["model"] = m.group(1)
        m = re.match(r'^Processor board ID\s+(\S+)', s)
        if m and not out["serial"]:
            out["serial"] = m.group(1)
        m = re.match(r'^System Serial Number\s*:\s*(\S+)', s)
        if m and not out["serial"]:
            out["serial"] = m.group(1)
    return out

def _parse_show_inventory(text):
    """Parse `show inventory` NAME/DESCR + PID/VID/SN line pairs."""
    rows = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^NAME:\s*"([^"]*)",\s*DESCR:\s*"([^"]*)"', s, re.IGNORECASE)
        if m:
            if cur: rows.append(cur)
            cur = {"name": m.group(1), "descr": m.group(2),
                   "pid": None, "vid": None, "sn": None}
            continue
        m = re.match(r'^PID:\s*([^,]*),\s*VID:\s*([^,]*),\s*SN:\s*(.*)$', s, re.IGNORECASE)
        if m and cur is not None:
            cur["pid"] = m.group(1).strip() or None
            cur["vid"] = m.group(2).strip() or None
            cur["sn"]  = m.group(3).strip() or None
    if cur: rows.append(cur)
    return rows

_INTF_STATUS_RE = re.compile(
    r'^(\S+)\s+(.*?)\s+'
    r'(connected|notconnect|disabled|err-disabled|inactive|monitoring|suspended)\s+'
    r'(\S+)\s+(\S+)\s+(\S+)\s+(.*)$', re.IGNORECASE)

def _parse_interfaces_status(text):
    """Parse the fixed-width `show interfaces status` table. The Name column
    is optional and free-form, so the row is anchored on the status keyword."""
    ports = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip(): continue
        if re.match(r'^\s*Port\s+Name\s+Status\s+Vlan\s+Duplex\s+Speed\s+Type\s*$',
                    s, re.IGNORECASE):
            continue
        m = _INTF_STATUS_RE.match(s)
        if not m: continue
        ports.append({
            "port":    m.group(1),
            "name":    m.group(2).strip(),
            "status":  m.group(3),
            "vlan":    m.group(4),
            "duplex":  m.group(5),
            "speed":   m.group(6),
            "type":    m.group(7).strip(),
        })
    return ports

_INTF_PREFIXES = (("TwentyFiveGigE", "Twe"), ("FortyGigabitEthernet", "Fo"),
                  ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
                  ("FastEthernet", "Fa"), ("HundredGigE", "Hu"),
                  ("Port-channel", "Po"), ("Ethernet", "Eth"))

def _short_intf(name):
    """GigabitEthernet1/0/1 -> Gi1/0/1 (CDP/LLDP use long names, the
    interfaces-status table uses short ones)."""
    n = (name or "").strip()
    for long, short in _INTF_PREFIXES:
        if n.startswith(long):
            return short + n[len(long):]
    return n

def _normalize_cdp_id(device_id):
    """CDP device IDs may carry a domain suffix (SW2.example.com) — strip it."""
    return (device_id or "").split(".")[0].strip()

def _parse_cdp_detail(text):
    """Parse `show cdp neighbors detail` into per-entry dicts."""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("---"):
            if cur and cur.get("local_intf"): entries.append(cur)
            cur = None
            continue
        m = re.match(r'^Device ID:\s*(\S+)', s, re.IGNORECASE)
        if m:
            if cur and cur.get("local_intf"): entries.append(cur)
            cur = {"device_id": m.group(1), "platform": "",
                   "local_intf": None, "remote_intf": None, "ip": None}
            continue
        if cur is None: continue
        m = re.match(r'^IP address:\s*(\S+)', s, re.IGNORECASE)
        if m and not cur["ip"]:
            cur["ip"] = m.group(1); continue
        m = re.match(r'^Platform:\s*([^,]+),', s, re.IGNORECASE)
        if m:
            cur["platform"] = m.group(1).strip(); continue
        m = re.match(r'^Interface:\s*(\S+?),\s*Port ID \(outgoing port\):\s*(\S+)',
                     s, re.IGNORECASE)
        if m:
            cur["local_intf"], cur["remote_intf"] = m.group(1), m.group(2)
            continue
    if cur and cur.get("local_intf"): entries.append(cur)
    return entries

def _parse_lldp_detail(text):
    """Parse `show lldp neighbors detail` into the same shape as CDP.
    Only used as a fallback when CDP yields no entries."""
    entries = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("---"):
            if cur and (cur.get("local_intf") or cur.get("device_id")):
                entries.append(cur)
            cur = None
            continue
        m = re.match(r'^Local Intf:\s*(\S+)', s, re.IGNORECASE)
        if m:
            if cur and (cur.get("local_intf") or cur.get("device_id")):
                entries.append(cur)
            cur = {"device_id": None, "platform": "",
                   "local_intf": m.group(1), "remote_intf": None, "ip": None}
            continue
        if cur is None: continue
        m = re.match(r'^System Name:\s*(.+)$', s, re.IGNORECASE)
        if m:
            cur["device_id"] = m.group(1).strip(); continue
        m = re.match(r'^Port id:\s*(\S+)', s, re.IGNORECASE)
        if m:
            cur["remote_intf"] = m.group(1); continue
    if cur and (cur.get("local_intf") or cur.get("device_id")):
        entries.append(cur)
    return [e for e in entries if e.get("device_id")]

def _eth_interface_type(speed, type_str=None):
    """Map interfaces-status speed/type to a NetBox interface type choice.
    Modular (SFP) ports map to the -x- types; unknown/auto -> 'other'."""
    s = (speed or "").lower().replace("a-", "").strip()
    t = (type_str or "").lower()
    sfpish = any(k in t for k in ("sfp", "gbic", "basesx", "baselx",
                                  "basesr", "baselr", "basezx"))
    if s == "100":   return "100base-tx"
    if s == "1000":  return "1000base-x-sfp" if sfpish else "1000base-t"
    if s in ("10g", "10000"):
        return "10gbase-x-sfpp" if sfpish else "10gbase-t"
    if s == "25g":   return "25gbase-x-sfp28"
    if s == "40g":   return "40gbase-x-qsfpp"
    if s == "100g":  return "100gbase-x-qsfp28"
    return "other"

# ── session (netmiko) ────────────────────────────────────────────────────────

class CiscoSwitchSession:
    """Thin netmiko wrapper for Catalyst IOS/IOS-XE. netmiko owns prompt
    detection, paging (`terminal length 0`) and privilege handling."""

    def __init__(self, ip, port=None, timeout=20):
        self.ip = ip
        self.port = port or CISCO_PORT
        self.timeout = timeout
        self.conn = None

    def login(self):
        self.conn = ConnectHandler(
            device_type="cisco_ios",
            host=self.ip, port=self.port,
            username=CISCO_USER, password=CISCO_PASS,
            conn_timeout=self.timeout, auth_timeout=self.timeout,
            banner_timeout=self.timeout,
        )

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

# ── probe + inventory collection ─────────────────────────────────────────────

def probe_cisco_switch(ip, retries=2, retry_delay=3):
    for attempt in range(1, retries + 1):
        if not is_port_open(ip, CISCO_PORT):
            if attempt < retries: time.sleep(retry_delay); continue
            return None
        sess = CiscoSwitchSession(ip)
        try:
            sess.login()
            try:
                info = _parse_show_version(sess.run("show version"))
                if not (info.get("serial") or info.get("model")):
                    raise RuntimeError("show version yielded no serial/model")
                model = (normalize_model(info.get("model"), CISCO_MODEL_MAP)
                         or info.get("model"))
                return {
                    "ip":           ip,
                    "host":         f"{ip}:{CISCO_PORT}",
                    "serial":       info.get("serial"),
                    "model":        model,
                    "hostname":     (info.get("hostname")
                                     or f"cisco-{ip.replace('.', '-')}"),
                    "manufacturer": "Cisco",
                    "firmware":     info.get("ios_version"),
                }
            finally:
                sess.logout()
        except Exception:
            if attempt < retries: time.sleep(retry_delay); continue
            return None
    return None

def _inventory_item_from_row(row, add_item):
    """Classify a `show inventory` row into PSU/Fan/SFP/Module and add it."""
    serial = (row.get("sn") or "").strip()
    if _invalid_serial(serial):
        return
    label = f"{row.get('name', '')} {row.get('descr', '')}".lower()
    if "power supply" in label:
        role = "PSU"
    elif "fan" in label:
        role = "Fan"
    elif "sfp" in label or "transceiver" in label or "gbic" in label:
        role = "SFP"
    else:
        role = "Module"
    add_item(
        name=str(row.get("descr") or row.get("name") or "Module")[:64],
        manufacturer="Cisco",
        part_number=row.get("pid") or None,
        serial=serial,
        description=f"Name={row.get('name')} Descr={row.get('descr')} VID={row.get('vid')}",
        role_id=netbox.get_or_create_inventory_role(role),
    )

def cisco_collect_inventory(ip):
    """Full inventory pull: identity, inventory rows, ports, CDP/LLDP neighbors."""
    sess = CiscoSwitchSession(ip)
    sess.login()
    try:
        ver = _parse_show_version(sess.run("show version"))
        inv_rows = _parse_show_inventory(sess.run("show inventory"))
        ports = _parse_interfaces_status(sess.run("show interfaces status"))
        log("INFO", f"  cisco show: {len(inv_rows)} inventory rows, {len(ports)} ports")

        try:
            neighbors = _parse_cdp_detail(sess.run("show cdp neighbors detail"))
            log("INFO", f"  cdp neighbors: {len(neighbors)}")
        except Exception as exc:
            neighbors = []
            log("WARN", f"  show cdp neighbors detail failed: {exc}")
        if not neighbors:
            try:
                neighbors = _parse_lldp_detail(sess.run("show lldp neighbors detail"))
                log("INFO", f"  lldp neighbors: {len(neighbors)}")
            except Exception as exc:
                log("WARN", f"  show lldp neighbors detail failed: {exc}")

        inventory = {}
        add_item = _make_add_item(inventory)
        for row in inv_rows:
            _inventory_item_from_row(row, add_item)

        summary = {
            "serial":    ver.get("serial"),
            "model":     (normalize_model(ver.get("model"), CISCO_MODEL_MAP)
                          or ver.get("model")),
            "firmware":  ver.get("ios_version"),
            "hostname":  (ver.get("hostname") or "").strip(),
            "port_count": len(ports),
        }
        return {"summary": summary, "ports": ports,
                "neighbors": neighbors, "inventory": inventory}
    finally:
        sess.logout()

# ── interfaces ───────────────────────────────────────────────────────────────

def sync_cisco_interfaces(dev_id, ports):
    """Create/update NetBox interfaces per switchport; delete stale ones.
    Description carries status/vlan/duplex/speed + the port's description."""
    api = netbox.get_netbox()
    existing = {}
    for iface in list(api.dcim.interfaces.filter(device_id=dev_id)):
        existing[str(iface.name)] = iface

    seen = set()
    for p in ports:
        name = p["port"]
        seen.add(name)
        desc_parts = [f"status={p.get('status')}", f"vlan={p.get('vlan')}",
                      f"duplex={p.get('duplex')}", f"speed={p.get('speed')}"]
        if p.get("name"):
            desc_parts.append(p["name"])
        payload = {
            "device":     dev_id,
            "name":       name,
            "type":       _eth_interface_type(p.get("speed"), p.get("type")),
            "enabled":    p.get("status", "").lower() == "connected",
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

    for name, iface in existing.items():
        if name not in seen:
            try: iface.delete()
            except Exception: pass
