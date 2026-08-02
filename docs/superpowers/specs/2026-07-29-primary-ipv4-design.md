# Primary IPv4 for discovered devices — design spec

**Date:** 2026-07-29
**Status:** Approved scope (brainstorming complete, pending implementation plan)
**Scope:** For every discovered device (all four families), create/update an IPAM IP address for the management IP the tool connects to, and set it as the device's **primary IPv4** in NetBox. Airflow and platform fields are explicitly **out of scope** (user decision).

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| Scope | All four families; servers use their BMC/iLO IP (the only IP known) |
| Airflow | **Skipped** (user decision) |
| Platform | **Skipped** (user decision) |
| IP mask | Derived from the scan range containing the IP (its prefix length); `/32` fallback |
| Interface assignment | None — the IP is only pointed to by `primary_ip4`, not assigned to an interface (we don't know the real management interface) |
| New config | **None** (zero-config behavior) |

## 2. Design

### Helper (new, `netbox_sync/netbox.py`)

```python
def ensure_primary_ip(dev_id, ip, hostname=None) -> int | None
```

Behavior:
1. **Find existing** IPAM address: `api.ipam.ip_addresses.filter(address=ip)` —
   if any exist (any mask), reuse the first one as-is (never fight an
   existing record's mask or data).
2. **Otherwise create**: `{"address": f"{ip}/{prefixlen}", "status": "active",
   "description": "netbox-sync: mgmt", "dns_name": dns}` where
   - `prefixlen` = `_mgmt_prefixlen(ip)` — the prefix length of the first
     configured scan range (BMC/STORAGE/SAN/CISCO, in that order) containing
     the IP; `32` if none contains it.
   - `dns` = hostname lowercased and sanitized to `[a-z0-9.-]` (skipped when
     empty/invalid).
3. **Assign**: `api.dcim.devices.update([{"id": dev_id, "primary_ip4": ip_id}])`
   — only when it differs from the device's current `primary_ip4` (avoids a
   useless write on every run).

`_mgmt_prefixlen(ip)` lives in `netbox_sync/utils.py` and reads the four range
lists from config. Containment uses `ipaddress` (mixed-version-safe via
try/except, consistent with `resolve_site`).

### Call sites (`netbox_sync/sync.py`)

Immediately after each `dev_id = ensure_*_device(probe)` in `run_sync`:

```python
try:
    ensure_primary_ip(dev_id, probe["ip"], probe.get("hostname"))
except Exception as e:
    log("WARN", f"  primary IPv4 sync failed for {ip}: {e}")
```

Failures here never abort the device's sync (WARN and continue — consistent
with the per-step isolation pattern).

### Error handling

- IPAM lookup/create failures → WARN, device sync continues.
- Duplicate addresses in IPAM (allowed by default in NetBox) → first reused.
- Device IP changes between runs → `primary_ip4` repointed to the new IP.
  The stale old IPAM record is left in place (see non-goals).

## 3. Testing

Fake `ipam.ip_addresses` + `dcim.devices` endpoints (existing harness):
- No existing IP → created with range-derived mask (`/24` from a containing
  range, `/32` when no range matches), marker description, sanitized dns_name;
  `primary_ip4` set on the device.
- Existing IP (different mask/data) → reused unchanged, `primary_ip4` set.
- `primary_ip4` already correct → no device write.
- `_mgmt_prefixlen`: containing range's prefix used; `/32` fallback.

## 4. Docs

- README (EN+FA): short paragraph in "How it works" — every device gets its
  management IP recorded in IPAM and set as primary IPv4; mask derived from
  the scan range (`/32` fallback).

## 5. Non-goals (YAGNI)

- No airflow, no platform fields.
- No interface assignment of the management IP (unknown which interface is mgmt).
- No cleanup/deprecation of stale IPAM records after a device IP change.
- No DNS resolution / reverse-PTR validation.
