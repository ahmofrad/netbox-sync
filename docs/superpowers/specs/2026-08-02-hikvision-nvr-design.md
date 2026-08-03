# Hikvision NVR Source — Design Spec

Date: 2026-08-02 (revised 2026-08-03)

## Goal

Import Hikvision NVRs into NetBox as **devices**. Each connected IP camera is
**its own NetBox device** (not an inventory item on the NVR), linked to the
parent NVR via a `cam_nvr` custom field. Each camera's management IP is set as
its primary IPv4.

> **Revision (2026-08-03):** the original design modeled cameras as *inventory
> items* on the NVR with marker-owned IPAM records. Per user request, cameras
> are now first-class devices. The `sync_inventory`/`sync_camera_ips` approach
> was replaced by `ensure_camera_device` + `ensure_primary_ip`, and the `Camera`
> role moved from an inventory-item role to a device role.

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
- `ensure_camera_device(cam, nvr_name)` → device id. Each camera is its own
  device; identity is the camera serial (match order: serial → name+site+role).
  Role from `DEFAULT_HIKVISION_CAMERA_ROLE` (default `"Camera"`). Writes
  `cam_ip`, `cam_mac`, `cam_enabled` (online), `cam_nvr` (parent NVR name),
  `cam_channel`, `cam_model`, `cam_serial`. The NVR API does not expose camera
  MACs, so `cam_mac` stays empty unless supplied.
- `mark_camera_offline(dev_id, name)` — `status=offline`, `cam_enabled=False`.
  Cameras no longer reported by an NVR are marked offline, never deleted.

## Camera IPs — primary IP per camera device

Each camera's management IP is set as its **primary IPv4** via the shared
`ensure_primary_ip(dev_id, ip, name)` (on the synthetic `mgmt` interface), the
same pattern as Ruckus APs. No separate marker-owned IPAM records are created.

## Config — `config.py`

- `HIKVISION_USER`, `HIKVISION_PASS`
- `HIKVISION_PORT = int(os.getenv("HIKVISION_PORT", "80"))`
- `HIKVISION_RANGES = _parse_ranges("HIKVISION_RANGES", [])` (opt-in, disabled
  by default)
- `DEFAULT_HIKVISION_ROLE = os.getenv("DEFAULT_HIKVISION_ROLE", "NVR")`
- `DEFAULT_HIKVISION_CAMERA_ROLE = os.getenv("DEFAULT_HIKVISION_CAMERA_ROLE", "Camera")`
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
5. Per camera: `ensure_camera_device(cam, nvr_name)` → camera device id, then
   `ensure_primary_ip(cam_dev, cam.ip, cam.name)`. Track seen serials.
6. Camera offline sweep: cameras under this NVR (`cf_cam_nvr`) whose serial was
   not seen are marked offline via `mark_camera_offline`.
7. `_offline_sweep(..., bool(HIKVISION_RANGES), "cf_nvr_enabled", "nvr_ip",
   live_ips, mark_hikvision_offline, "NVR")`.

## Non-goals

- No camera interfaces beyond the synthetic `mgmt` carrier for the primary IP.
- No camera credential management / ONVIF probing — the NVR is the sole source.
- No RTSP/stream config, recording state, or storage sync.
- No basic-auth fallback (digest only); no HTTPS (these NVRs serve plain HTTP).
- No parent/child device-bay relationship (the NVR is not a chassis); the link
  is the `cam_nvr` custom field.

## Files touched

- `netbox_sync/collectors/hikvision.py` (new)
- `netbox_sync/config.py`
- `netbox_sync/scanner.py`
- `netbox_sync/netbox.py` (`ensure_hikvision_device`, `mark_hikvision_offline`,
  `ensure_camera_device`, `mark_camera_offline`)
- `netbox_sync/sync.py` (processing block + camera/NVR offline sweeps)
- `.env.example`, `README.md` (config table + custom-field rows)
- `tests/` (parsers + ensure/sync logic)
