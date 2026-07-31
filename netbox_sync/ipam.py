"""IPAM helpers: prefix derivation and prefix/IP-address reconciliation.

Markers: prefixes carry `netbox-sync: last seen <hostname>` in their
description; host addresses carry `netbox-sync: if <hostname> <iface>`.
Only marked objects are ever refreshed or deleted."""
import ipaddress

from netbox_sync import netbox
from netbox_sync.config import log

IPAM_PREFIX_MARKER = "netbox-sync: last seen "
IPAM_HOST_MARKER = "netbox-sync: if "
NAT_MARKER = "netbox-sync: nat "


def _nat_desc(entry):
    parts = [f"{NAT_MARKER}{entry.get('name')}"]
    if entry.get("protocol"):
        parts.append(str(entry["protocol"]).upper())
    if entry.get("extport") is not None:
        parts.append(f"port {entry['extport']}")
    if entry.get("mappedport") is not None \
            and entry.get("mappedport") != entry.get("extport"):
        parts.append(f"-> {entry['mappedport']}")
    if entry.get("status"):
        parts.append(str(entry["status"]))
    return " ".join(parts)[:200]


def _ensure_ipam_address(address, description):
    """Get-or-create a plain IPAM address (any-mask reuse); refresh the
    description only on marked records."""
    api = netbox.get_netbox()
    bare = address.split("/")[0]
    existing = list(api.ipam.ip_addresses.filter(address=bare))
    if existing:
        rec = existing[0]
        if (getattr(rec, "description", None) or "").startswith("netbox-sync:"):
            api.ipam.ip_addresses.update([{"id": rec.id,
                                           "description": description}])
        return rec
    return api.ipam.ip_addresses.create({
        "address": address, "status": "active", "description": description})


def sync_nat_ips(vips, pools):
    """Model FortiGate NAT in IPAM: external VIP addresses with nat_inside
    pointing at their mapped (inside) addresses; SNAT pools as plain
    addresses. Returns the set of bare IPs seen (for sweeping)."""
    api = netbox.get_netbox()
    seen = set()
    for v in vips or []:
        ext_ip = (v.get("extip") or "").strip()
        if not ext_ip:
            continue
        seen.add(ext_ip)
        inside_id = None
        mapped = [str(m).strip() for m in (v.get("mappedip") or []) if str(m).strip()]
        if mapped:
            inside_ip = mapped[0]
            inside_id = _ensure_ipam_address(
                f"{inside_ip}/32",
                f"{NAT_MARKER}inside {v.get('name')}").id
            seen.add(inside_ip)
        ext_rec = _ensure_ipam_address(f"{ext_ip}/32", _nat_desc(v))
        if inside_id:
            api.ipam.ip_addresses.update(
                [{"id": ext_rec.id, "nat_inside": inside_id}])
    for p in pools or []:
        sip = (p.get("startip") or "").strip()
        if not sip:
            continue
        seen.add(sip)
        _ensure_ipam_address(
            f"{sip}/32",
            f"{NAT_MARKER}{p.get('name')} (pool {p.get('type')}, "
            f"{sip}-{p.get('endip')})")
    return seen


def sweep_nat_ips(seen_bare_ips):
    """Delete marker-owned NAT addresses not seen this run. Global scope —
    the caller passes the union of all FortiGates' seen IPs."""
    api = netbox.get_netbox()
    for ip in list(api.ipam.ip_addresses.filter()):
        desc = getattr(ip, "description", None) or ""
        if not desc.startswith(NAT_MARKER):
            continue
        if str(ip.address).split("/")[0] not in seen_bare_ips:
            try:
                ip.delete()
                log("INFO", f"  nat IP {ip.address} deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete nat IP {ip.address}: {exc}")


def _prefix_from_ip(ip_field):
    """Derive the prefix string from a FortiGate-style "A.B.C.D M.M.M.M"
    field; None for empty/zero/invalid."""
    if not ip_field:
        return None
    parts = str(ip_field).split()
    if len(parts) < 2:
        return None
    addr, mask = parts[0], parts[1]
    if addr == "0.0.0.0":
        return None
    try:
        return ipaddress.ip_interface(f"{addr}/{mask}").network.with_prefixlen
    except ValueError:
        return None


def _iface_addr_with_prefixlen(ip_field):
    """"A.B.C.D M.M.M.M" -> ("A.B.C.D/len", "A.B.C.D"); (None, None) if
    empty/zero/invalid."""
    if not ip_field:
        return None, None
    parts = str(ip_field).split()
    if len(parts) < 2 or parts[0] == "0.0.0.0":
        return None, None
    try:
        iface = ipaddress.ip_interface(f"{parts[0]}/{parts[1]}")
    except ValueError:
        return None, None
    return iface.with_prefixlen, str(iface.ip)


def _prefix_masklen(prefix_str):
    return ipaddress.ip_network(prefix_str, strict=False).prefixlen


def ensure_prefix(prefix_str, site_id, vlan_id, hostname, iface_name):
    """Get-or-create a prefix in IPAM. Marker-owned records are refreshed
    (site/vlan/description); manual records are reused untouched."""
    api = netbox.get_netbox()
    payload = {"site": site_id,
               "status": "active",
               "description": f"{IPAM_PREFIX_MARKER}{hostname} {iface_name}"}
    if vlan_id:
        payload["vlan"] = vlan_id
    existing = api.ipam.prefixes.get(prefix=prefix_str)
    if existing:
        if (existing.description or "").startswith("netbox-sync:"):
            api.ipam.prefixes.update([{"id": existing.id, **payload}])
        return existing.id
    return api.ipam.prefixes.create(
        {"prefix": prefix_str, **payload}).id


def _containing_prefix(ip):
    """Longest-prefix match for a bare IP across all IPAM prefixes
    (client-side; works on any NetBox version)."""
    api = netbox.get_netbox()
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return None
    best = None
    best_len = -1
    for p in api.ipam.prefixes.filter():
        try:
            net = ipaddress.ip_network(p.prefix, strict=False)
        except (ValueError, TypeError):
            continue
        if addr in net and net.prefixlen > best_len:
            best, best_len = p, net.prefixlen
    return best


def ensure_host_ip(dev_id, address, iface_name, hostname, iface_label):
    """Create/update a host address and assign it to the named interface
    (NetBox requires assignment for primary IPs; description only touches
    marked-or-created records — manual IPs keep theirs)."""
    api = netbox.get_netbox()
    bare = address.split("/")[0]
    iface = api.dcim.interfaces.get(device_id=dev_id, name=iface_name)
    if iface is None:
        log("WARN", f"  host IP {address}: interface {iface_name} not found "
                    f"on device id={dev_id} — skipped")
        return None
    desc = f"{IPAM_HOST_MARKER}{hostname} {iface_label}"
    existing = list(api.ipam.ip_addresses.filter(address=bare))
    if existing:
        ip_id = existing[0].id
        upd = {"id": ip_id,
               "assigned_object_type": "dcim.interface",
               "assigned_object_id": iface.id}
        if (getattr(existing[0], "description", None) or "").startswith("netbox-sync:"):
            upd["description"] = desc
        api.ipam.ip_addresses.update([upd])
        return ip_id
    return api.ipam.ip_addresses.create({
        "address": address, "status": "active", "description": desc,
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": iface.id}).id


def sweep_stale_prefixes(site_id, seen_prefixes):
    """Delete marker-owned prefixes at the site that were not seen this
    run. Manual prefixes are never touched."""
    api = netbox.get_netbox()
    for p in list(api.ipam.prefixes.filter(site_id=site_id)):
        if not (getattr(p, "description", None) or "").startswith("netbox-sync:"):
            continue
        if p.prefix not in seen_prefixes:
            try:
                p.delete()
                log("INFO", f"  prefix {p.prefix} (site {site_id}) deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete prefix {p.prefix}: {exc}")


def sweep_stale_host_ips(dev_id, seen_addresses):
    """Delete marker-owned host addresses ('netbox-sync: if ') on the
    device not seen this run. 'netbox-sync: mgmt' addresses and manual
    ones are never touched."""
    api = netbox.get_netbox()
    for ip in list(api.ipam.ip_addresses.filter(device_id=dev_id)):
        desc = getattr(ip, "description", None) or ""
        if not desc.startswith(IPAM_HOST_MARKER):
            continue
        if str(ip.address).split("/")[0] not in seen_addresses:
            try:
                ip.delete()
                log("INFO", f"  host IP {ip.address} (device {dev_id}) deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete host IP {ip.address}: {exc}")
