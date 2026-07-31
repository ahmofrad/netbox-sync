"""IPAM helpers: prefix derivation and prefix/IP-address reconciliation.

Markers: prefixes carry `netbox-sync: last seen <hostname>` in their
description; host addresses carry `netbox-sync: if <hostname> <iface>`.
Only marked objects are ever refreshed or deleted."""
import ipaddress

from netbox_sync import netbox
from netbox_sync.config import log

IPAM_PREFIX_MARKER = "netbox-sync: last seen "
IPAM_HOST_MARKER = "netbox-sync: if "


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
