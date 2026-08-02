# IP-range-based site assignment — design spec

**Date:** 2026-07-29
**Status:** Approved (brainstorming complete, pending implementation plan)
**Scope:** Assign NetBox sites from the device's IP address (CIDR map) with hostname-keyword matching as fallback.

---

## 1. Decisions (locked during brainstorming)

| Question | Decision |
|----------|----------|
| Precedence | **IP range first**, hostname keyword fallback, then `SITE_UNKNOWN` |
| Overlap semantics | **Longest-prefix-match** (most specific containing range wins) |
| Config shape | One global `SITE_IP_MAP` for all four device families (no per-family maps) |
| Backward compatibility | Empty `SITE_IP_MAP` → behavior identical to today |

## 2. Current state

`resolve_site_from_name(hostname)` in `netbox_sync/utils.py` iterates
`SITE_KEYWORD_MAP` (substring, case-insensitive, first match wins) and falls
back to `SITE_UNKNOWN` (= `DEFAULT_SITE_NAME` or `"Unknown"`). It receives no
IP, so sites cannot be derived from the network a device lives on.

## 3. Design

### Resolution chain (per device, in `ensure_*_device`)

```
site = first (network, site) in SITE_IP_MAP (sorted by prefixlen desc)
       where device_ip in network
       ── none match ──►
site = first (keyword, site) in SITE_KEYWORD_MAP
       where keyword in hostname.lower()
       ── none match ──►
site = SITE_UNKNOWN
```

### Config (`netbox_sync/config.py`)

```dotenv
SITE_IP_MAP=172.31.0.0/16:HQ,172.31.1.0/24:Branch-F10
```

- Parsed at import into `SITE_IP_MAP: list[tuple[ipaddress.IPv4Network, str]]`
  via a helper `_parse_site_ip_map(env_value)` that:
  - splits on commas, then each pair on the **first** `:` (same rule as
    `SITE_KEYWORD_MAP`; site names therefore cannot contain colons)
  - skips empty pairs and pairs without `:`
  - builds networks with `ipaddress.ip_network(cidr, strict=False)`
  - logs a WARN and skips entries that fail to parse
- Result is **pre-sorted by `prefixlen` descending** (ties: as listed), so
  iteration order = most-specific-first; resolution is a simple first-hit loop.

### Code changes

| File | Change |
|------|--------|
| `netbox_sync/config.py` | `SITE_IP_MAP` parsing + sorted structure |
| `netbox_sync/utils.py` | `resolve_site_from_name(hostname)` → `resolve_site(hostname, ip)`; IP map first, keyword map second, `SITE_UNKNOWN` last; invalid/unparseable `ip` strings skip the IP stage silently |
| `netbox_sync/netbox.py` | 4 call sites (`ensure_server/storage/san_switch/cisco_device`) pass `probe.get("hostname")` + `probe["ip"]` |
| `tests/test_helpers.py` | update existing site tests to new signature |
| `tests/test_netbox_sync.py` | config parsing tests (reload-based, like the ranges tests) |
| `.env.example`, `README.md` (EN+FA) | document `SITE_IP_MAP`, precedence, longest-prefix semantics |

### Error handling

- Malformed map entry (no colon, bad CIDR): WARN at startup, entry skipped.
- Device IP missing/invalid (should not happen — probes always carry IPs):
  IP stage skipped, hostname stage still applies.
- IPv6 CIDRs are accepted by `ip_network`; device IPs are IPv4 in practice —
  mixed-version containment checks raise `TypeError`, caught and treated as
  no-match for that entry.

## 4. Testing

- `resolve_site`: IP hit beats keyword hit; longest-prefix beats broad range;
  no-IP-match falls to keyword; no match at all falls to `SITE_UNKNOWN`;
  invalid IP string tolerated; empty map behaves like today.
- `_parse_site_ip_map` via `importlib.reload(cfg)` (same pattern as the
  ranges tests): valid parse + sort order, malformed entry skipped with WARN,
  empty var → empty list.
- Existing `test_ensure_cisco_device_creates_with_custom_fields` and the other
  ensure tests keep passing (site resolution is mocked there).

## 5. Non-goals (YAGNI)

- No per-family IP maps, no site assignment from NetBox-side data, no VLAN/
  prefix-based site lookup against NetBox's own IPAM, no GUI/reporting.
