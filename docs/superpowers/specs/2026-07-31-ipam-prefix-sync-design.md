# IPAM prefix & host-address sync — design spec

**Date:** 2026-07-31
**Status:** Approved (scope: full v1, sweep stale marked)
**Scope:** Build the IPAM layer from collected data: prefixes from FortiGate interface IPs (with real masks), FortiGate gateway addresses on subinterfaces, Cisco SVI host addresses, with marker-owned reconciliation.

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| Prefix source | FortiGate cmdb interface `ip` (`A.B.C.D M.M.M.M` → prefix via `ip_interface`) |
| Gateway addresses | FortiGate subinterface IPs, real mask, assigned to the subinterface |
| Cisco host addresses | SVI IPs from `show ip interface brief` → longest-prefix containment match; no match → skip (DEBUG) |
| VLAN linkage | Prefix gets `vlan` from the resolved vid map |
| Markers | Prefix: `netbox-sync: last seen <hostname>`; host IP: `netbox-sync: if <hostname> <iface>` (mgmt IPs use `netbox-sync: mgmt` — never swept by IPAM sweeps) |
| Stale handling | Sweep marked prefixes/addresses not seen this run (per site); manual untouched |
| VRF | Skipped (all interfaces are `vrf 0` = global table) |
| Module | New `netbox_sync/ipam.py` (IPAM helpers; collectors stay protocol-focused) |

## 2. Data flows

**FortiGate** (after VLAN resolution + interface sync, so vid map and subifs exist):
- For each port with a parseable `ip` (non-`0.0.0.0`): prefix = `ip_interface(addr/mask).network.with_prefixlen`
- `ensure_prefix(prefix_str, site_id, vlan_id, hostname, iface)` → create (marked) / refresh (marked) / reuse untouched (manual)
- `ensure_host_ip(dev_id, addr_with_mask, iface_name, description)` → find-by-address-any-mask (reuse) or create with the interface's real mask; assign to the interface (required for NetBox validity, same rule as primary IP)
- seen sets for the sweep

**Cisco** (after SVI/primary-IP handling):
- For each `ip_brief` entry (SVI): containment via `api.ipam.prefixes.filter(contains=ip)` → longest match wins → host address (mask = prefix's prefixlen) assigned to the SVI; no match → DEBUG skip

**Sweep (end of run, per site):**
- Marked prefixes not in seen → delete (NetBox keeps child IPs when a prefix is deleted)
- Marked host IPs (`netbox-sync: if `) per device not in seen → delete
- `netbox-sync: mgmt` addresses are excluded from the sweep by marker shape

## 3. Testing

- Prefix derivation: valid, mask forms, `0.0.0.0` skipped
- `ensure_prefix`: create with site+vlan, refresh marked, manual reused untouched
- Host IP: create with real mask + assignment, reuse, longest-prefix containment choice
- Sweeps: keep seen / delete stale / keep manual / keep mgmt

## 4. Non-goals

- VRFs, IP ranges, aggregate (parent) prefixes, DHCP/pool data, DNS sync
- Prefixes for non-FortiGate sources (Cisco SVIs join FortiGate-derived prefixes only)
