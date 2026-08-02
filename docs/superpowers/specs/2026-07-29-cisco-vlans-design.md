# Cisco VLAN sync — design spec

**Date:** 2026-07-29
**Status:** Approved (brainstorming complete)
**Scope:** Sync VLAN objects from Cisco Catalyst switches into NetBox IPAM (per-site), wire VLAN↔interface linkage (access untagged, trunk native + tagged), and sweep stale marker-owned VLANs per site.

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| Scope | VLAN records **and** interface assignment (access untagged; trunk native + tagged/tagged-all) |
| Site model | **Per-site** VLANs — uniqueness per `(site, vid)`; site taken from the device |
| Stale handling | **Delete stale marked VLANs**, per site, only when no processed switch at that site reported the vid this run |
| Ownership | Marker prefix `netbox-sync:` in description; manual VLANs never modified or deleted (but their IDs are still used for interface linkage) |
| New config | None |

## 2. Data flow (per switch, in `run_sync` Cisco block)

```
cisco_collect_inventory  (+ show vlan brief, + show interfaces trunk)
        │
sync_cisco_vlans(site_id, hostname, vlans)      → {vid: netbox_id}
sync_cisco_interfaces(dev_id, ports)            (existing)
sync_interface_vlans(dev_id, ports, trunks, vid_map)
sync_inventory / sync_cdp_cables                (existing)
        │
after the Cisco loop, per site:
sweep_stale_vlans(site_id, seen_vids_union)
```

site_id is read from the device record (`api.dcim.devices.get(id=dev_id).site.id`); switches without a site skip VLAN work (WARN).

## 3. Parsers (pure, fixture-tested)

- `_parse_vlan_brief` → `[{vid, name, status}]` (row regex anchored on the status keyword; ports column ignored)
- `_parse_interfaces_trunk` → `[{port, mode, native, allowed, active}]` from the sectioned trunk tables (main table + "Vlans allowed on trunk" + "Vlans allowed and active in management domain")
- `_expand_vlan_list(spec)` → `set[int]` for `"1,10,20-25"`, or `None` for default-all (`"1-4094"`, `"all"`, empty) which maps to NetBox `mode="tagged-all"`

## 4. Sync rules

**`sync_cisco_vlans`** — per vid:
- exists & marker-owned → update name/status/marker description (`netbox-sync: last seen <hostname>`)
- exists & unmarked → **left untouched** (manual VLAN), id still used for linkage
- missing → create with `site`, `status=active`, marker description

**`sync_interface_vlans`** — per port:
- numeric VLAN column → `mode=access`, `untagged_vlan=vid_map[vid]` (skip when vid unknown)
- trunk (in trunk table or VLAN column `trunk`) → native → `untagged_vlan` when known; active (fallback allowed) list: default-all → `mode=tagged-all`; else `mode=tagged` + `tagged_vlans` (expanded, filtered to known vids)
- `routed` → no linkage

**`sweep_stale_vlans(site_id, seen)`** — after the Cisco loop:
- marker-owned && vid ∉ seen → delete (NetBox auto-clears interface links)
- seen or unmarked → keep

## 5. Error handling & testing

- Per-switch try/except + WARN; sweep errors logged ERROR, never abort the run.
- Tests: three parser tests with canonical IOS-XE fixtures; VLAN sync create/update/manual-reuse with fake `ipam.vlans`; interface linkage for access/trunk/tagged-all/routed; sweep keep/delete/manual cases.

## 6. Non-goals (YAGNI)

- No VLAN groups, no voice VLANs, no QinQ, no VLANs from non-Cisco families (Brocade FOS VLANs are unrelated to IPAM Ethernet VLANs), no prefix/IPAM subnet sync, no per-interface description changes beyond current behavior.
