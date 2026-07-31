"""IPAM helpers: prefix derivation and prefix/IP-address reconciliation.

Markers: prefixes carry `netbox-sync: last seen <hostname>` in their
description; host addresses carry `netbox-sync: if <hostname> <iface>`.
Only marked objects are ever refreshed or deleted."""
import ipaddress
import re

from netbox_sync import netbox
from netbox_sync.config import log, SITE_IP_MAP

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


def _find_ip_by_bare(api, bare):
    """Find an IPAM address by its bare IP (no mask) — client-side match so
    it behaves identically on any NetBox address-filter semantics."""
    for r in api.ipam.ip_addresses.filter():
        if str(r.address).split("/")[0] == bare:
            return r
    return None


def _ensure_ipam_address(address, description):
    """Get-or-create a plain IPAM address (any-mask reuse); refresh the
    description only on marked records."""
    api = netbox.get_netbox()
    bare = address.split("/")[0]
    existing = _find_ip_by_bare(api, bare)
    if existing:
        if (getattr(existing, "description", None) or "").startswith("netbox-sync:"):
            api.ipam.ip_addresses.update([{"id": existing.id,
                                           "description": description}])
        return existing
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


def _svc_ports(extport):
    """Parse a FortiGate extport (int, range string, or junk) into service
    port ints. Ranges expand (capped); unparseable -> []."""
    if extport is None:
        return []
    s = str(extport).strip()
    m = re.match(r'^(\d+)$', s)
    if m:
        return [int(m.group(1))]
    m = re.match(r'^(\d+)\s*-\s*(\d+)$', s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi >= lo and hi - lo <= 64:
            return list(range(lo, hi + 1))
    return []


def _create_service(api, payload):
    """Create a service across NetBox API variants: 4.x wants
    parent_object_type/parent_object_id; 3.x wants plain device FK."""
    try:
        return api.ipam.services.create(payload)
    except Exception as exc:
        if "parent_object" in str(exc):
            fallback = {k: v for k, v in payload.items()
                        if k not in ("parent_object_type", "parent_object_id")}
            fallback["device"] = payload["parent_object_id"]
            return api.ipam.services.create(fallback)
        raise


def sync_nat_services(dev_id, vips):
    """Per-port NAT fidelity: one NetBox Service per VIP (name, protocol,
    port) linked to the external IP address, mapped backend in the
    description. Returns the set of service names seen (for sweeping)."""
    api = netbox.get_netbox()
    seen = set()
    for v in vips or []:
        name = (v.get("name") or "").strip()
        ext_ip = (v.get("extip") or "").strip()
        if not name or not ext_ip:
            continue
        seen.add(name)
        ports = _svc_ports(v.get("extport"))
        if not ports:
            # Static NAT without port mapping — the address-level nat_inside
            # (from sync_nat_ips) already models it; no Service needed.
            log("DEBUG", f"  nat service {name}: no ports — handled via nat_inside")
            continue
        mapped = [str(m).strip() for m in (v.get("mappedip") or []) if str(m).strip()]
        mappedport = v.get("mappedport") or v.get("extport")
        desc = (f"{NAT_MARKER}{name} -> "
                f"{mapped[0] if mapped else '?'}:{mappedport}"
                + (f" ({v.get('status')})" if v.get("status") else ""))[:200]
        payload = {
            "parent_object_type": "dcim.device",
            "parent_object_id": dev_id,
            "name": name,
            "protocol": (v.get("protocol") or "tcp").lower(),
            "ports": ports,
            "description": desc,
        }
        ext_rec = _find_ip_by_bare(api, ext_ip)
        if ext_rec:
            payload["ipaddresses"] = [ext_rec.id]
        existing = api.ipam.services.get(device_id=dev_id, name=name)
        if existing:
            update_payload = {k: v for k, v in payload.items()
                              if k not in ("parent_object_type",
                                           "parent_object_id", "device")}
            api.ipam.services.update([{"id": existing.id, **update_payload}])
        else:
            try:
                _create_service(api, payload)
            except Exception as exc:
                log("WARN", f"  nat service {name}: create failed: {exc}")
    return seen


def sweep_nat_services(dev_id, seen_names):
    """Delete marker-owned NAT services on the device not seen this run."""
    api = netbox.get_netbox()
    for s in list(api.ipam.services.filter(device_id=dev_id)):
        if not (getattr(s, "description", None) or "").startswith(NAT_MARKER):
            continue
        if s.name not in seen_names:
            try:
                s.delete()
                log("INFO", f"  nat service {s.name} (device {dev_id}) deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete nat service {s.name}: {exc}")


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


def _prefix_scope_kwargs(site_id):
    """NetBox 4.x prefixes scope generically (scope_type/scope_id); 3.x used
    a direct site FK (handled by the fallback paths below)."""
    return {"scope_type": "dcim.site", "scope_id": site_id}


def _prefix_site_id(p):
    """Site id of a prefix across API variants: scope_id (4.x) or site.id."""
    sid = getattr(p, "scope_id", None)
    if sid is not None:
        return sid
    site = getattr(p, "site", None)
    return getattr(site, "id", None) if site is not None else None


def ensure_prefix(prefix_str, site_id, vlan_id, hostname, iface_name,
                  status="active"):
    """Get-or-create a prefix in IPAM. Marker-owned records are refreshed
    (scope/vlan/description); manual records are reused untouched. Parents
    use status='container', discovered prefixes 'active'."""
    api = netbox.get_netbox()
    payload = {"status": status,
               "description": f"{IPAM_PREFIX_MARKER}{hostname} {iface_name}",
               **_prefix_scope_kwargs(site_id)}
    if vlan_id:
        payload["vlan"] = vlan_id
    existing = api.ipam.prefixes.get(prefix=prefix_str)
    if existing:
        if (getattr(existing, "description", None) or "").startswith("netbox-sync:"):
            try:
                api.ipam.prefixes.update([{"id": existing.id, **payload}])
            except Exception as exc:
                if "scope" in str(exc) or "site" in str(exc):
                    legacy = {k: v for k, v in payload.items()
                              if k not in ("scope_type", "scope_id")}
                    legacy["site"] = site_id
                    api.ipam.prefixes.update([{"id": existing.id, **legacy}])
                else:
                    raise
        return existing.id
    try:
        return api.ipam.prefixes.create({"prefix": prefix_str, **payload}).id
    except Exception as exc:
        if "scope" in str(exc) or "site" in str(exc):
            legacy = {k: v for k, v in payload.items()
                      if k not in ("scope_type", "scope_id")}
            legacy["site"] = site_id
            return api.ipam.prefixes.create({"prefix": prefix_str, **legacy}).id
        raise


def sync_parent_prefixes():
    """Create/update one 'container' prefix per SITE_IP_MAP entry (the
    parents that discovered prefixes nest under). Returns
    {site_id: {prefix_str}} for sweep-seen bookkeeping."""
    seen = {}
    for net, site_name in SITE_IP_MAP:
        site_id = netbox.get_or_create_site(site_name)
        ensure_prefix(str(net), site_id, None, "parent", site_name,
                      status="container")
        seen.setdefault(site_id, set()).add(str(net))
    return seen


def sweep_stale_parents():
    """Delete marker-owned parent (container) prefixes whose SITE_IP_MAP
    entry no longer exists."""
    api = netbox.get_netbox()
    valid = {str(n) for n, _ in SITE_IP_MAP}
    for p in list(api.ipam.prefixes.filter(status="container")):
        desc = getattr(p, "description", None) or ""
        if not desc.startswith(f"{IPAM_PREFIX_MARKER}parent"):
            continue
        if p.prefix not in valid:
            try:
                p.delete()
                log("INFO", f"  parent prefix {p.prefix} deleted — removed from SITE_IP_MAP")
            except Exception as exc:
                log("WARN", f"  could not delete parent prefix {p.prefix}: {exc}")


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
    run. Manual prefixes are never touched. Site matching is client-side
    (scope_id on 4.x, site FK on 3.x)."""
    api = netbox.get_netbox()
    for p in list(api.ipam.prefixes.filter()):
        if _prefix_site_id(p) != site_id:
            continue
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
