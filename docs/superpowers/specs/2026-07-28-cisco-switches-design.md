# Cisco Catalyst switch support — design spec

**Date:** 2026-07-28
**Status:** Approved (brainstorming complete, pending implementation plan)
**Scope:** Add a fourth device family — Cisco Catalyst (IOS / IOS-XE) switches — to the netbox-sync tool, including device records, component inventory, per-port interfaces, and CDP/LLDP-derived cables in NetBox.

---

## 1. Decisions (locked during brainstorming)

| Question | Decision |
|----------|----------|
| Platform | Cisco Catalyst only — classic IOS **and** IOS-XE CLI dialects (no NX-OS) |
| Access method | SSH CLI via **netmiko** (`device_type="cisco_ios"`) |
| Sync scope | Device + component inventory + interfaces + **CDP/LLDP cabling** |
| Approach | Brocade-mirror collector; marked-cable reconciliation (Approach A) |
| Family activation | **Opt-in**: `CISCO_RANGES` empty by default → family disabled |

Rationale for netmiko over raw paramiko: IOS pages output (`--More--`) and prompt/paging handling differs per platform; netmiko owns those edge cases, is pure Python, and builds on paramiko (already a dependency).

## 2. Architecture

| File | Change |
|------|--------|
| `netbox_sync/collectors/cisco.py` | **New** — netmiko session, CLI parsers, `probe_cisco_switch`, `cisco_collect_inventory`, `sync_cisco_interfaces`, `sync_cdp_cables` |
| `netbox_sync/config.py` | `CISCO_USER`, `CISCO_PASS`, `CISCO_PORT` (default 22), `CISCO_RANGES` (default **empty**), `DEFAULT_CISCO_ROLE` (default `Switch`) |
| `netbox_sync/models.py` | `CISCO_MODEL_MAP` — light normalization (Cisco PIDs such as `WS-C3850-48P` are near-canonical) |
| `netbox_sync/netbox.py` | `ensure_cisco_device`, `mark_cisco_offline` (same placement as other families) |
| `netbox_sync/scanner.py` | Fourth family in `scan_all`; skips IPs already found as server/storage/SAN |
| `netbox_sync/sync.py` | Fourth processing block + offline check via `cf_cisco_enabled` |
| `requirements.txt` | add `netmiko` |
| `.env.example`, `README.md` (EN+FA) | new env vars, custom fields, supported-hardware section |
| `tests/test_cisco_parsers.py` | new; cable-matcher tests extend `tests/test_netbox_sync.py` |

Dependency flow stays acyclic: config ← utils ← netbox ← collectors ← scanner ← sync.

## 3. Data flow

```
CISCO_RANGES ──► probe_cisco_switch(ip)      [ThreadPool, SCAN_WORKERS]
                   TCP/22 open? → netmiko → show version
                   → {ip, host, serial, model, hostname, manufacturer: "Cisco", firmware}
                        │
  per switch:
    ensure_cisco_device(probe)   serial match → fallback name+site+role
    cisco_collect_inventory(ip):
        show version              → summary
        show inventory            → items: PSU / Fan / Module / SFP (serial-keyed)
        show interfaces status    → ports
        show cdp neighbors detail → neighbors
        (if CDP empty → show lldp neighbors detail)
                        │
    update custom fields: cisco_ip, cisco_enabled, cisco_firmware,
                          cisco_model, cisco_port_count
    sync_cisco_interfaces(dev_id, ports)   NetBox interfaces
    sync_inventory(dev_id, items)          shared serial-keyed reconciliation
    sync_cdp_cables(dev_id, neighbors)     marked NetBox cables (§5)
                        │
  offline: devices where cf_cisco_enabled=True whose cisco_ip was not seen
           this scan → _check_offline (OFFLINE_THRESHOLD), mark_cisco_offline
```

## 4. Session & parsers

**Session:** netmiko `ConnectHandler(device_type="cisco_ios", host=..., username=CISCO_USER, password=CISCO_PASS, port=CISCO_PORT, conn_timeout=...)`. Commands: the five listed above. netmiko handles `terminal length 0`, prompt detection and paging.

**Parsers** (pure functions, fixture-tested):

- `_parse_show_version` → hostname, model, serial, ios_version. Must accept both dialects:
  - classic IOS: `Cisco IOS Software, ... Version 15.0(2)SE`, `Model number: WS-C2960X-48FPS-L`, `Processor board ID FOC12345678`, `<hostname> uptime is ...`
  - IOS-XE: `Cisco IOS Software [Fuji], ... Version 16.9.4`, `Model Number: C9300-48U`, `System Serial Number: XXX`
- `_parse_show_inventory` → list of `{name, descr, pid, vid, sn}` from `NAME:/DESCR:/PID:/VID:/SN:` blocks. Role assignment:
  - name/descr contains "power supply" / starts `PS ` → role `PSU`
  - contains "fan" → role `Fan`
  - contains "transceiver" / "SFP" → role `SFP`
  - everything else with a valid serial → role `Module`
  - Roles resolved by name via `get_or_create_inventory_role` (auto-created).
- `_parse_interfaces_status` → `{port, name, status, vlan, duplex, speed, type}` from the fixed-width table.
- `_parse_cdp_detail` → per-entry `{device_id, platform, local_intf, remote_intf, ip}` from `show cdp neighbors detail` (`Device ID:`, `Interface: Gi1/0/1, Port ID (outgoing port): Gi0/1`, `IP address:`).
- `_parse_lldp_detail` → same shape from `show lldp neighbors detail` (`System Name:`, `Port id:`, `Interface:`). Used only when CDP yields zero entries.
- Model strings from `show version` are normalized through `CISCO_MODEL_MAP` (`normalize_model`) exactly like the other families.
- `_eth_interface_type(speed, type_str)` → NetBox interface type: `100base-tx`, `1000base-t`, `10gbase-t`, `10gbase-sr` (SFP-type strings), `25gbase-sr`, `40gbase-qsfpp`; anything unknown → `other`.

**Stacks** (3850/9300 multi-member): v1 treats the stack as **one device** (single management IP). Device serial = master/first member serial; every member chassis becomes a `Module` inventory item with its own serial.

## 5. Cable reconciliation

Per switch, after interfaces exist:

```
for each neighbor entry:
    local_intf  → interface on THIS device (exists — just synced)
    neighbor    → normalize(device_id): strip domain suffix, lowercase
                  → NetBox device lookup by name (cached per run)
    remote_intf → interface named remote_intf on that device
    any lookup fails → skip + DEBUG log
      (notably Cisco↔server links: servers have no NetBox interfaces in
      this tool — server NICs are inventory items, so these skip by design)

create/update:
    if a MARKED cable ("netbox-sync:" description) already terminates on
      either interface → update its description/endpoints
    if an UNMARKED cable already terminates on either interface → leave it
      untouched and DEBUG-log a conflict note (manual cabling wins)
    else create:
        a_terminations = [{object_type: "dcim.interface", object_id: local_id}]
        b_terminations = [{object_type: "dcim.interface", object_id: remote_id}]
    description = "netbox-sync: cdp <swA> <intfA> <-> <swB> <intfB>"   # ownership marker

reconcile (this switch only):
    cables terminating on this device's interfaces whose description
    starts with "netbox-sync:" and were NOT seen this run → delete
    Manually documented cables (no marker) are never touched.
```

Notes:
- Cable API payload targets NetBox 3.3+ (`a_terminations`/`b_terminations`); README states v3.x support. First real run verifies against the production instance.
- A physical link is discovered from both ends; the dedupe check prevents duplicate cables.

## 6. Config & NetBox prerequisites

**New env vars** (all optional; documented in `.env.example` and README EN+FA):

| Variable | Default | Description |
|----------|---------|-------------|
| `CISCO_USER` | — | SSH username (required only if `CISCO_RANGES` set) |
| `CISCO_PASS` | — | SSH password |
| `CISCO_PORT` | `22` | SSH port |
| `CISCO_RANGES` | *(empty)* | Comma-separated CIDRs; empty disables the family |
| `DEFAULT_CISCO_ROLE` | `Switch` | NetBox device role for Cisco switches |

`CISCO_USER`/`CISCO_PASS` are **not** in `REQUIRED_ENV_VARS`; they are validated only when Cisco ranges are configured (error at startup if ranges set but creds missing).

**Custom fields** (create in NetBox, `dcim | device`; README tables EN+FA updated):

| Field | Type | Label |
|-------|------|-------|
| `cisco_ip` | Text | Cisco switch IP |
| `cisco_enabled` | Boolean | Cisco switch enabled |
| `cisco_firmware` | Text | IOS version |
| `cisco_model` | Text | Model |
| `cisco_port_count` | Integer | Port count |

Manufacturer `Cisco` and device role `Switch` are auto-created. Device matching: serial first, fallback name+site+role (existing pattern).

## 7. Error handling

- Per-switch `try/except` isolation — one failing switch never aborts the run.
- netmiko auth/timeout errors → probe retries (same counts as Brocade), then None.
- Cable API failures → WARN log, never abort.
- Unresolvable neighbors → DEBUG log only (expected for non-NetBox neighbors).
- New inventory roles (`Fan`, `Module`) auto-created by name; `PSU`/`SFP` reuse existing roles.

## 8. Testing

- `tests/test_cisco_parsers.py` — recorded fixtures for classic IOS (12/15) and IOS-XE (16.x) formats of: `show version`, `show inventory`, `show interfaces status`, `show cdp neighbors detail`, `show lldp neighbors detail`. Also `_eth_interface_type` cases.
- `tests/test_netbox_sync.py` — cable matcher with the fake-pynetbox harness:
  resolve-both-ends → create; existing cable → update, not duplicate;
  unresolvable neighbor → skip; stale marked cable → delete; unmarked cable → preserved.
- Session-level behavior (netmiko) is not unit-tested (no hardware); parser and reconciliation logic is.
- Fixtures use canonical documented Cisco output. If a real switch's format differs, adjust parsers from sanitized real output (same workflow used for Brocade).

## 9. Non-goals (YAGNI)

- No Cisco↔server cabling (would require creating interfaces on servers — separate design).
- No VLAN/STP/MAC-table sync, no config management/backups, no SNMP, no NX-OS.
- No stack-member explosion into multiple devices (v1: stack = one device).

## 10. Backward compatibility

- Existing deployments without Cisco switches: no behavior change (family disabled by default; no new required env vars).
- `python sync_all_to_netbox.py` invocation unchanged.
- Only new dependency: `netmiko` (added to `requirements.txt`).
