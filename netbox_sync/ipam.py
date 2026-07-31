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
