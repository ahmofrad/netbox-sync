# FortiGate family support — design spec

**Date:** 2026-07-30
**Status:** Approved
**Scope:** Add FortiGate firewalls as a fifth device family — REST API primary (device, interfaces, VLANs), SSH extras (LLDP cabling, SFP transceivers). Per-device API tokens from a token file.

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| Transport | **REST API primary** (`/api/v2/monitor`, `/api/v2/cmdb`) + **SSH extras** (netmiko `fortinet`) for LLDP + SFPs |
| API credentials | **Per-device token file** (`fortigate_tokens.txt`, repo root, gitignored; `FORTIGATE_TOKEN_FILE` override): `<ip[:port]> <token>` per line, `#` comments allowed |
| SSH credentials | Shared env `FORTIGATE_USER`/`FORTIGATE_PASS` |
| Role | `Firewall` (auto-created, `DEFAULT_FORTIGATE_ROLE`) |
| Family activation | Opt-in: `FORTIGATE_RANGES` empty = disabled; creds validated only when set |
| VLAN grouping | Per-device group via existing `ensure_vlan_group(site_id, hostname)` — FortiGates have no shared VLAN database |
| Cabling | LLDP neighbors → existing cable reconciliation (generalized with a protocol label) |
| VDOMs | v1 queries `vdom=root` only |

## 2. Components

**Config** — `_load_fortigate_tokens(path)` → `{ip: (port, token)}` (skips
comments/blanks/malformed lines with WARN); env: `FORTIGATE_USER/PASS`,
`FORTIGATE_PORT` (443), `FORTIGATE_SSH_PORT` (22), `FORTIGATE_TOKEN_FILE`
(default `<repo>/fortigate_tokens.txt`), `FORTIGATE_RANGES`,
`DEFAULT_FORTIGATE_ROLE`. `_validate_config`: ranges set → require SSH creds
AND non-empty token map.

**Collector (`netbox_sync/collectors/fortigate.py`)**
- `FortiGateSession(ip, port, token)` — requests session, `Authorization: Bearer <token>`, `verify=False`, `get(path)` helper.
- `probe_fortigate(ip)` — token lookup (skip+DEBUG when absent), quick port check, `GET /api/v2/monitor/system/status` → `{ip, host, serial, model, hostname, manufacturer: "Fortinet", firmware}`.
- `fortigate_collect(ip)`:
  - API: `/monitor/system/status` (identity), `/monitor/system/interface` (link state + speed), `/cmdb/system/interface?vdom=root` (type, vlanid, ip)
  - SSH: `diagnose lldp neighbor-summary` → neighbors, `diagnose sys transceiver list` → SFP items
  - Returns `{summary, ports, vlans, neighbors, inventory}`
- Pure mappers (fixture-tested): `_fg_status(json)`, `_fg_interfaces(monitor_json, cmdb_json)`, `_fg_vlans(cmdb_json)`, `_parse_lldp_summary(text)`, `_parse_transceivers(text)`.

**NetBox side**
- `netbox.py`: `ensure_fortigate_device` (custom fields `fortigate_ip/_enabled/_firmware/_model/_port_count`), `mark_fortigate_offline`.
- `collectors/fortigate.py`: `sync_fortigate_interfaces` (bulk pattern; physical ports typed by speed, VLAN subinterfaces as `virtual` + `untagged_vlan`).
- **Reuse:** `ensure_vlan_group` + `sync_cisco_vlans` (group keyed by hostname), `sync_inventory`, `ensure_primary_ip`, `resolve_site`.
- `sync_cdp_cables` generalized: `protocol="cdp"` parameter used in the cable description (`lldp` for FortiGate).
- `sync.py`: fifth processing block + offline sweep (`fortigate_enabled`); scanner: fifth probe pool.
- `models.py`: `FORTIGATE_MODEL_MAP` (thin passthrough map).

## 3. Config/env surface

| Variable | Default | Notes |
|----------|---------|-------|
| `FORTIGATE_RANGES` | *(empty)* | empty = disabled |
| `FORTIGATE_API_TOKEN` | — | **not used** (replaced by token file) |
| `FORTIGATE_TOKEN_FILE` | `fortigate_tokens.txt` | per-device tokens, gitignored |
| `FORTIGATE_USER` / `FORTIGATE_PASS` | — | SSH extras; required when ranges set |
| `FORTIGATE_PORT` | `443` | API port (overridable per-device in the file) |
| `FORTIGATE_SSH_PORT` | `22` | SSH port |
| `DEFAULT_FORTIGATE_ROLE` | `Firewall` | device role |

## 4. Testing

- Token-file parsing: comments, blanks, `ip:port`, malformed lines skipped.
- API mappers with recorded JSON fixtures (status, monitor+cmdb interface merge, vlan extraction).
- SSH parsers with plausible FortiOS fixtures (real output may need adjustment — same workflow as Brocade).
- `ensure_fortigate_device` creation payload; `sync_fortigate_interfaces` bulk behavior + VLAN subinterface linkage; cable reuse with `protocol="lldp"`.

## 5. Future phases (reminder — implement after phase 1)

- **HA cluster merging** (one NetBox device per HA pair; parse `get system ha status`).
- **Firewall policies / NAT / address objects** sync.
- **Access-port VLAN maps** (FortiGates don't expose switchport tables like Catalysts — needs a different source, e.g. per-interface config or hardware-switch config parsing).

## 6. Non-goals (phase 1)

Multi-VDOM queries, HA awareness, policies/NAT/objects, switchport-VLAN mapping, IPsec/SSL VPN data, anything beyond the five data flows above.
