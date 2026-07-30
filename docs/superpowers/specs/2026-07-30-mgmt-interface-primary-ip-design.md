# Interface labels + primary IP on the real management interface — design spec

**Date:** 2026-07-30
**Status:** Approved
**Scope:** (1) FortiGate interface `alias` → NetBox interface `label`. (2) Assign the primary IPv4 to the device's *real* management interface (FortiGate VLAN subinterface, Cisco SVI) instead of the synthetic `mgmt` interface when the carrier interface can be identified.

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| FortiGate label | cmdb `alias` → interface `label` (when non-empty; ≤64 chars) |
| FortiGate mgmt carrier | Match probe IP against cmdb interface `ip` first token → that subinterface |
| Cisco mgmt carrier | New command `show ip interface brief` → match mgmt IP to an interface (e.g. `Vlan54`) |
| Missing SVI | Created as `virtual`, `untagged_vlan` parsed from its `VlanNN` name when the vid is in the device's VLAN group map |
| Fallback | Synthetic `mgmt` interface (unchanged) when no carrier matches |
| Servers/storage/SAN | Unchanged — synthetic `mgmt` is the correct model there |

## 2. Components

**FortiGate collector** — `_fg_interfaces` also captures `alias`; `sync_fortigate_interfaces` sets `"label": alias` when non-empty.

**`netbox.ensure_primary_ip(dev_id, ip, hostname=None, iface_name=None)`** — new optional `iface_name`: when given, resolve that interface on the device and assign the IP to it; if not found, WARN + fall back to the synthetic `mgmt` interface. Rest unchanged.

**Cisco collector** — `_parse_ip_interface_brief(text)` → `{intf_name: ip}` (skips `unassigned`); `cisco_collect_inventory` runs `show ip interface brief` and returns it as `"ip_brief"`. New helper `ensure_svi_interface(dev_id, name, vid_map)` — get-or-create a `virtual` interface for an SVI, `untagged_vlan` parsed from `VlanNN` when present in `vid_map`.

**`run_sync` ordering** (FortiGate and Cisco blocks): primary-IP assignment moves **after** interface sync (the carrier interface must exist first). Carrier selection:
- FortiGate: first port whose cmdb `ip` first token equals the probe IP.
- Cisco: `ip_brief` lookup of the probe IP → interface name; if the name isn't among the just-synced switchports, `ensure_svi_interface` creates it.

Servers/storage/SAN blocks keep the current call position and behavior.

## 3. Testing

- `_fg_interfaces` captures alias; `sync_fortigate_interfaces` writes `label`.
- `ensure_primary_ip` with `iface_name`: assigns to the named interface; missing name → mgmt fallback + WARN; existing behavior (no iface_name) unchanged.
- `_parse_ip_interface_brief` fixture (Vlan SVIs + unassigned rows).
- `ensure_svi_interface`: creates virtual interface with parsed vid, reuses existing.

## 4. Docs

README (EN+FA): label mapping note + primary-IP carrier behavior (real SVI/subinterface when identifiable, synthetic `mgmt` fallback).

## 5. Non-goals

- No full L3-interface sync (only the mgmt-IP carrier SVI is created, not every SVI).
- No secondary IPs, no VRF handling, no `ip interface` data for FortiGate aggregates/tunnels.
