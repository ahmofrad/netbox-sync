# FortiGate VLAN resolution (match-first, MAC disambiguation) — design spec

**Date:** 2026-07-30
**Status:** Approved
**Scope:** FortiGate VLANs resolve against the switches' existing VLANs instead of duplicating them: unique vid match → reuse; no match → create in the per-device group; overlap → disambiguate via switch MAC tables.

---

## 1. Decisions (user-directed, feasibility verified live)

| Rule | Behavior |
|------|----------|
| vid in exactly **one** group at the site | Reuse that VLAN record — nothing created |
| vid in **no** group | Create in the per-device group (hostname key, current behavior) |
| vid in **multiple** groups (overlap) | Query candidate domains' switches with `show mac address-table address <mac>`; the switch returning that vid wins its group; no hit → per-device group + WARN |
| Per-subif MAC source | `fnsysctl ifconfig -a` on the FortiGate (verified: per-parent MACs, `(mac, vid)` pairs unique per domain) |
| Migration | Automatic: shared vids are swept out of the per-device group by the existing sweep (its seen-set now holds only created vids) |

## 2. Verified facts (live, 2026-07-30, FG180F)

- cmdb interface `macaddr` is `00:00:00:00:00:00` (unset) — not usable.
- `fnsysctl ifconfig <name>` works, incl. quoted names with spaces; `-a` returns all interfaces. VLAN subifs inherit their parent interface's MAC (e.g. "AP MGMT" == "Core Switch" = `00:09:0F:09:00:24`), but distinct parents → distinct MACs; `(mac, vid)` pairs remain unique per domain.
- Cisco CAM lookup: `show mac address-table address 0009.0f09.0024` returns `vid, mac, type, port` rows.

## 3. Components

**Parsers (pure, fixture-tested)**
- `_parse_ifconfig_a(text)` → `{iface_name: mac}` from `Name\tLink encap:Ethernet  HWaddr xx:xx:...` blocks (lowercased colon form).
- `_mac_to_cisco(mac)` → dotted triplet (`0009.0f09.0024`).
- `_parse_mac_table_entry(text)` → `[{vid, mac, port}]`.

**Collector** — `fortigate_collect` runs `fnsysctl ifconfig -a` (via `_ssh_run_or_none`) and returns `"vlan_macs": {vid: mac}` (matched to cmdb VLAN rows by interface name).

**Resolution (`collectors/fortigate.py`)**
```python
resolve_fortigate_vlans(site_vlan_index, vlans, vlan_macs, mac_lookup)
    -> (vid_map, missing_vlans)
```
`site_vlan_index`: `{vid: [(group_id, vlan_id), ...]}` built per site by `_site_vlan_index(api, site_id)` (cisco.py) from marker-owned groups. `mac_lookup(vid, mac) -> group_id | None` is injected by run_sync (tries each candidate group's member switches via `_cisco_mac_lookup(ip, mac)`).

**run_sync**
- Cisco block tracks `switch_group_ips[group_id] -> [switch ips]` for disambiguation targeting.
- FortiGate block: resolve → reused map + missing; missing synced into the per-device group via `ensure_vlan_group(site, hostname)` + `sync_cisco_vlans`; `group_vlan_seen` for that group gets **only the created vids** (shared vids are swept out — the migration); interfaces link via the combined map.
- When the Cisco family is disabled / no switches seen, the site index is empty → everything goes to the per-device group (current behavior).

## 4. Testing

- Parser fixtures from the live device (`fnsysctl ifconfig -a` blocks, Cisco MAC-table row).
- `resolve_fortigate_vlans`: unique/none/multi-hit/multi-miss paths with an injected fake `mac_lookup`.
- `_site_vlan_index` with the fake harness.

## 5. Non-goals

- No global MAC-table sync (only targeted `address <mac>` lookups during disambiguation).
- No HA-specific MAC handling (HA pairs share MACs by design = same domain).
- No FortiGate↔switch topology inference beyond disambiguation.
