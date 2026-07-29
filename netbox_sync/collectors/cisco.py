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

def _parse_vlan_brief(text):
    """Parse `show vlan brief`: vid, name, status. Ports column ignored."""
    vlans = []
    for line in text.splitlines():
        s = line.rstrip()
        m = re.match(r'^(\d+)\s+(.+?)\s+(active|act/unsup|suspended|shutdown)\b',
                     s, re.IGNORECASE)
        if not m: continue
        vlans.append({"vid": int(m.group(1)), "name": m.group(2).strip(),
                      "status": m.group(3).lower()})
    return vlans

def _expand_vlan_list(spec):
    """Expand "1,10,20-25,100" into a set of vids. Returns None for the
    default all-VLANs range — the caller maps that to mode 'tagged-all'
    instead of an explicit tagged list."""
    s = (spec or "").strip().lower()
    if not s or s in ("all", "1-4094", "1-4096"):
        return None
    vids = set()
    for part in s.split(","):
        part = part.strip()
        if not part: continue
        m = re.match(r'^(\d+)(?:-(\d+))?$', part)
        if not m: continue
        lo = int(m.group(1)); hi = int(m.group(2) or lo)
        vids.update(range(lo, min(hi, 4094) + 1))
    return vids

def _parse_interfaces_trunk(text):
    """Parse `show interfaces trunk` into per-port dicts. Tracks the
    sectioned tables: main (mode/native), 'Vlans allowed on trunk',
    'Vlans allowed and active in management domain'."""
    trunks = {}
    section = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip(): continue
        low = s.lower()
        if "vlans allowed on trunk" in low:
            section = "allowed"; continue
        if "vlans allowed and active" in low:
            section = "active"; continue
        if "vlans in spanning tree" in low:
            section = None; continue
        if re.match(r'^Port\s+Mode\s+Encapsulation', s, re.IGNORECASE):
            section = "main"; continue
        m = re.match(r'^(\S+)\s+(on|desirable|auto|trunk|off|nonegotiate)\s+'
                     r'(\S+)\s+(\S+)\s+(\d+)\s*$', s, re.IGNORECASE)
        if m and section in (None, "main"):
            trunks[m.group(1)] = {"port": m.group(1), "mode": m.group(2).lower(),
                                  "native": int(m.group(5)),
                                  "allowed": None, "active": None}
            continue
        m2 = re.match(r'^(\S+)\s+([\d,\-]+)\s*$', s)
        if m2 and section in ("allowed", "active") and m2.group(1) in trunks:
            trunks[m2.group(1)][section] = m2.group(2)
    return list(trunks.values())

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

        try:
            vlans = _parse_vlan_brief(sess.run("show vlan brief"))
            log("INFO", f"  vlans: {len(vlans)}")
        except Exception as exc:
            vlans = []
            log("WARN", f"  show vlan brief failed: {exc}")
        try:
            trunks = _parse_interfaces_trunk(sess.run("show interfaces trunk"))
            log("INFO", f"  trunks: {len(trunks)}")
        except Exception as exc:
            trunks = []
            log("WARN", f"  show interfaces trunk failed: {exc}")

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
                "neighbors": neighbors, "inventory": inventory,
                "vlans": vlans, "trunks": trunks}
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
            if getattr(iface, "mgmt_only", False):
                continue   # never delete management interfaces
            try: iface.delete()
            except Exception: pass

# ── VLANs ────────────────────────────────────────────────────────────────────

# Ownership marker: only VLANs whose description starts with this prefix are
# updated/deleted by the sync. Manual VLANs are never modified.
VLAN_MARKER = "netbox-sync:"

def sync_cisco_vlans(site_id, hostname, vlans):
    """Get-or-create each VLAN in IPAM for the site; refresh marker-owned
    records. Returns {vid: netbox_id} for interface linkage."""
    api = netbox.get_netbox()
    vid_map = {}
    for v in vlans:
        vid = v["vid"]
        payload = {"vid": vid, "name": v.get("name") or f"VLAN{vid:04d}",
                   "status": "active",
                   "description": f"{VLAN_MARKER} last seen {hostname}"}
        existing = api.ipam.vlans.get(vid=vid, site_id=site_id)
        if existing:
            if (existing.description or "").startswith(VLAN_MARKER):
                api.ipam.vlans.update([{"id": existing.id, **payload}])
            vid_map[vid] = existing.id
            continue
        try:
            vid_map[vid] = api.ipam.vlans.create({**payload, "site": site_id}).id
        except Exception as exc:
            log("WARN", f"  vlan {vid}: create failed on {hostname}: {exc}")
    return vid_map

def sync_interface_vlans(dev_id, ports, trunks, vid_map):
    """Wire VLAN linkage on switch interfaces: access untagged, trunk
    native + tagged (or tagged-all for the default range)."""
    api = netbox.get_netbox()
    by_name = {str(i.name): i
               for i in api.dcim.interfaces.filter(device_id=dev_id)}
    trunk_by_port = {t["port"]: t for t in trunks}
    for p in ports:
        iface = by_name.get(p["port"])
        if not iface: continue
        vlan_col = (p.get("vlan") or "").strip().lower()
        if vlan_col == "routed": continue
        t = trunk_by_port.get(p["port"])
        payload = None
        if t or vlan_col == "trunk":
            payload = {"id": iface.id}
            native = (t or {}).get("native")
            if native in vid_map:
                payload["untagged_vlan"] = vid_map[native]
            expanded = _expand_vlan_list((t or {}).get("active")
                                         or (t or {}).get("allowed"))
            if expanded is None:
                payload["mode"] = "tagged-all"
            else:
                payload["mode"] = "tagged"
                payload["tagged_vlans"] = [vid_map[v] for v in sorted(expanded)
                                           if v in vid_map]
        elif vlan_col.isdigit() and int(vlan_col) in vid_map:
            payload = {"id": iface.id, "mode": "access",
                       "untagged_vlan": vid_map[int(vlan_col)]}
        if payload:
            api.dcim.interfaces.update([payload])

def sweep_stale_vlans(site_id, seen_vids):
    """Delete marker-owned VLANs at the site that no processed switch
    reported this run. Manual (unmarked) VLANs are never touched."""
    api = netbox.get_netbox()
    for vlan in list(api.ipam.vlans.filter(site_id=site_id)):
        if not (vlan.description or "").startswith(VLAN_MARKER):
            continue
        if vlan.vid not in seen_vids:
            try:
                vlan.delete()
                log("INFO", f"  vlan {vlan.vid} (site {site_id}) deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete stale vlan {vlan.vid}: {exc}")

# ── CDP/LLDP cable reconciliation ────────────────────────────────────────────

# Ownership marker: only cables whose description starts with this prefix are
# managed (refreshed/deleted) by the sync. Manual cabling is never touched.
CABLE_MARKER = "netbox-sync:"

def _cable_iface_ids(cable):
    for t in (getattr(cable, "a_terminations", None) or []) + \
             (getattr(cable, "b_terminations", None) or []):
        oid = t.get("object_id") if isinstance(t, dict) else None
        if oid is not None:
            yield oid

def sync_cdp_cables(dev_id, neighbors):
    """Reconcile NetBox cables for one switch from CDP/LLDP neighbor data.

    Both ends must resolve to existing NetBox interfaces; anything else is
    skipped (DEBUG). Only marker-owned cables are managed."""
    api = netbox.get_netbox()
    local_ifaces = {str(i.name): i
                    for i in api.dcim.interfaces.filter(device_id=dev_id)}
    existing_cables = list(api.dcim.cables.filter(device_id=dev_id))
    marked = [c for c in existing_cables
              if (c.description or "").startswith(CABLE_MARKER)]
    unmarked = [c for c in existing_cables
                if not (c.description or "").startswith(CABLE_MARKER)]

    marked_by_iface = {}
    for c in marked:
        for oid in _cable_iface_ids(c):
            marked_by_iface.setdefault(oid, c)

    peer_dev_cache = {}
    seen_cable_ids = set()

    for n in neighbors:
        local = local_ifaces.get(_short_intf(n.get("local_intf")))
        if not local:
            log("DEBUG", f"  cdp: local iface {n.get('local_intf')} not found, skipping")
            continue
        peer_name = _normalize_cdp_id(n.get("device_id"))
        if not peer_name:
            continue
        if peer_name not in peer_dev_cache:
            try:
                peer_dev_cache[peer_name] = api.dcim.devices.get(name=peer_name)
            except Exception:
                peer_dev_cache[peer_name] = None
        peer_dev = peer_dev_cache[peer_name]
        if not peer_dev:
            log("DEBUG", f"  cdp: neighbor {peer_name} not in NetBox, skipping")
            continue
        peer_iface = api.dcim.interfaces.get(
            device_id=peer_dev.id, name=_short_intf(n.get("remote_intf")))
        if not peer_iface:
            log("DEBUG", f"  cdp: iface {n.get('remote_intf')} not found on "
                        f"{peer_name}, skipping")
            continue

        desc = (f"{CABLE_MARKER} cdp {local.name} <-> "
                f"{peer_name} {peer_iface.name}")
        existing = marked_by_iface.get(local.id) or marked_by_iface.get(peer_iface.id)
        if existing:
            seen_cable_ids.add(existing.id)
            api.dcim.cables.update([{"id": existing.id, "description": desc}])
            continue
        if any(local.id in _cable_iface_ids(c) or peer_iface.id in _cable_iface_ids(c)
               for c in unmarked):
            log("DEBUG", f"  cdp: manual cable exists on {local.name} or "
                        f"{peer_iface.name}, leaving untouched")
            continue
        try:
            cable = api.dcim.cables.create({
                "a_terminations": [{"object_type": "dcim.interface",
                                    "object_id": local.id}],
                "b_terminations": [{"object_type": "dcim.interface",
                                    "object_id": peer_iface.id}],
                "description": desc,
            })
            seen_cable_ids.add(cable.id)
            log("INFO", f"  cdp: cabled {local.name} <-> {peer_name} {peer_iface.name}")
        except Exception as exc:
            log("WARN", f"  cdp: could not create cable {local.name} "
                        f"<-> {peer_name} {peer_iface.name}: {exc}")

    for c in marked:
        if c.id not in seen_cable_ids:
            try:
                c.delete()
                log("INFO", f"  cdp: removed stale cable id={c.id}")
            except Exception: pass
