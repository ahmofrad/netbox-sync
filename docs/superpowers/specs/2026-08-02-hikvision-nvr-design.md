# Hikvision NVR Source — Design Spec

Date: 2026-08-02

## Goal

Import Hikvision NVRs into NetBox as **devices**, with their connected IP cameras
represented as **inventory items** on the parent NVR (not as separate NetBox
devices). Camera management IPs are registered in IPAM for subnet completeness.

## Verified facts

Hikvision ISAPI over HTTP digest auth (port 80) exposes:

- `GET /ISAPI/System/deviceInfo` → NVR identity: `deviceName`, `model`,
  `serialNumber`, `macAddress`, `firmwareVersion`, `deviceType` (`NVR`).
- `GET /ISAPI/ContentMgmt/InputProxy/channels` → `InputProxyChannelList` of
  cameras, each with `id` (channel number), `name`, and a
  `sourceInputPortDescriptor` holding `ipAddress`, `model`, `serialNumber`,
  `firmwareVersion`.
- `GET /ISAPI/ContentMgmt/InputProxy/channels/status` → per-channel `id` +
  `online` (`true`/`false`).

All XML is namespaced (`xmlns="http://www.isapi.org/ver20/XMLSchema"`); the
namespace must be stripped/ignored when parsing. `requests.auth.HTTPDigestAuth`
handles the digest handshake (confirmed available). Channel `id`s are not
contiguous (gaps where channels are unassigned).

## Collector — `netbox_sync/collectors/hikvision.py`

Follows the established collector contract (same shape as `msa.py` / `ruckus.py`).

- `class HikvisionSession` — wraps `requests.Session` with
  `HTTPDigestAuth(HIKVISION_USER, HIKVISION_PASS)`, `verify=False`. Methods:
  `get(path)` → raw XML text, `logout()` (no-op for HTTP).
- Pure parsers (namespace-stripped via `ElementTree` local-name handling):
  - `_parse_device_info(xml_text)` → `{name, model, serial, mac, firmware, device_type}`
  - `_parse_channels(xml_text)` → list of `{channel, name, ip, model, serial, firmware}`
  - `_parse_channel_status(xml_text)` → `{channel_id: online_bool}`
- `probe_hikvision(ip, retries=2, retry_delay=3)` → identity dict or `None`:
  `{"ip", "host": f"{ip}:{HIKVISION_PORT}", "serial", "model", "hostname",
  "reported_ip", "mac", "manufacturer": "Hikvision", "firmware"}`.
  Gated on `is_port_open(ip, HIKVISION_PORT)` and a successful `deviceInfo` fetch.
- `hikvision_collect(ip)` → `{"summary": {...}, "cameras": [...]}`.
  Merges channels + status by channel id; each camera gains `online` (default
  `False` if no status entry). Cameras with an empty serial are kept but keyed
  by channel (see below).

## NetBox side — `netbox.py`

- `ensure_hikvision_device(probe)` → device id. Match order: serial →
  name+site+role. Role from `DEFAULT_HIKVISION_ROLE` (default `"NVR"`).
  Manufacturer `"Hikvision"`. Writes custom fields
  `nvr_ip`, `nvr_enabled`, `nvr_model`, `nvr_firmware`, `nvr_camera_count`.
- `mark_hikvision_offline(dev_id, name)` — `status=offline`, `nvr_enabled=False`.
- Cameras → existing shared `sync_inventory(dev_id, {serial: item})`:
  - **Key = camera serial** when present; fall back to `ch<channel>` when the
    NVR reports an empty serial (keeps the item stable per channel).
  - `name` = camera `name` (e.g. `23092-GF Security`), truncated to 64 chars.
  - `part_id` = camera `model`; `serial` = camera serial.
  - `role` = `get_or_create_inventory_role("Camera")`.
  - `description` = `Channel=N IP=x.x.x.x FW=... Status=online|offline` (≤200).
  - `sync_inventory` already deletes stale serials → removed cameras drop off
    automatically. No extra sweep needed.

## Camera IPs in IPAM — `ipam.py`

Cameras are not device interfaces, so their IPs are plain IPAM addresses
(marker-owned, like NAT addresses), **not** assigned to any device interface.

- New marker `CAM_MARKER = "netbox-sync: cam "`.
- `sync_camera_ips(nvr_name, cameras)` → for each camera with an `ip`, ensure a
  plain IPAM address `<ip>/32` with description `netbox-sync: cam <nvr_name>
  <camera name> (ch<N>)`. Reuses existing address records; only touches
  marker-owned ones. Returns the set of bare camera IPs seen.
- `sweep_camera_ips(seen_bare_ips)` — delete marker-owned camera IPs not seen
  this run. Global scope (union across all NVRs).

## Config — `config.py`

- `HIKVISION_USER`, `HIKVISION_PASS`
- `HIKVISION_PORT = int(os.getenv("HIKVISION_PORT", "80"))`
- `HIKVISION_RANGES = _parse_ranges("HIKVISION_RANGES", [])` (opt-in, disabled
  by default)
- `DEFAULT_HIKVISION_ROLE = os.getenv("DEFAULT_HIKVISION_ROLE", "NVR")`
- Validation: when `HIKVISION_RANGES` is set, require `HIKVISION_USER`/`HIKVISION_PASS`.

## Scanner — `scanner.py`

Add `"hikvision_nvrs": []` to `scan_all()`'s result dict and a scan block
guarded on `HIKVISION_RANGES`, probing `probe_hikvision` over
`expand_ranges(HIKVISION_RANGES)` minus already-claimed IPs (same pattern as
the other opt-in families).

## Orchestration — `sync.py`

Per probe in `found["hikvision_nvrs"]`:

1. `hikvision_collect(ip)` (skip on error).
2. `ensure_hikvision_device(probe)` → `dev_id`.
3. Device CF update (`nvr_*` fields, `status=active`).
4. `ensure_primary_ip(dev_id, ip, hostname)`.
5. `sync_inventory(dev_id, camera_items)`.
6. `sync_camera_ips(nvr_name, cameras)` → union seen IPs; after all NVRs,
   `sweep_camera_ips(seen)`.
7. `_offline_sweep(..., bool(HIKVISION_RANGES), "cf_nvr_enabled", "nvr_ip",
   live_ips, mark_hikvision_offline, "NVR")`.

## Non-goals

- No per-camera NetBox devices or interfaces.
- No camera credential management / ONVIF probing — the NVR is the sole source.
- No RTSP/stream config, recording state, or storage sync.
- No basic-auth fallback (digest only); no HTTPS (these NVRs serve plain HTTP).

## Files touched

- `netbox_sync/collectors/hikvision.py` (new)
- `netbox_sync/config.py`
- `netbox_sync/scanner.py`
- `netbox_sync/netbox.py` (`ensure_hikvision_device`, `mark_hikvision_offline`)
- `netbox_sync/ipam.py` (`sync_camera_ips`, `sweep_camera_ips`, `CAM_MARKER`)
- `netbox_sync/sync.py` (processing block + offline sweep)
- `.env.example`, `README.md` (config table + custom-field/inventory-role rows)
- `tests/` (parsers + ensure/sync logic)
