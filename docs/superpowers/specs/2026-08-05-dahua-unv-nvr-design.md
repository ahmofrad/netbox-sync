# Dahua + Uniview (UNV) NVR Sources — Design Spec

Date: 2026-08-05

## Goal

Import Dahua and Uniview NVRs into NetBox as devices, each connected camera as
its own device (serial identity, `cam_*` custom fields, `cam_nvr` parent link,
primary IP) — the same model as the Hikvision family. Cameras with a usable
MAC participate in the existing MAC-table camera→switch cabling automatically.

All endpoints below were probed and verified live against the production NVRs
(Dahua `192.168.252.5`, UNV `192.168.112.66`) on 2026-08-05.

## Verified facts

### Dahua (HTTP digest auth, port 80, CGI API)

- `GET /cgi-bin/magicBox.cgi?action=getSystemInfo` → `serialNumber`,
  `deviceType` (numeric), `updateSerial` (model series, e.g. `NVR6XX-4KS2`),
  `processor`.
- `GET /cgi-bin/magicBox.cgi?action=getDeviceClass` → `class=NVR` (probe gate).
- `GET /cgi-bin/magicBox.cgi?action=getSoftwareVersion` → firmware
  (`4.002.0000000.2.R,build:2023-02-20`).
- `GET /cgi-bin/magicBox.cgi?action=getMachineName` → `name=NVR` (generic; a
  generic/empty name falls back to `dahua-<ip-dashed>`).
- `GET /cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice` →
  `table.RemoteDevice.uuid:..._<N>.<Field>=<value>` rows. Slot index N maps
  1:1 to channel N+1 (verified: 32 slots == 32 channel titles). Per slot:
  `Address` (camera IP), `SerialNo`, `DeviceType` (**camera model**),
  `Version` (camera firmware), `Enable`, `Vendor`/`ProtocolType` (e.g.
  Onvif), `Mac` (often empty or `ff:ff:ff:ff:ff:ff` — **unreliable; treat
  empty/ff:ff:ff:ff:ff:ff as absent**).
- `GET /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle` →
  `table.ChannelTitle[<N>].Name` (channel N+1's display name).
- Channel online-state configs return HTTP 403 for the available account →
  **no per-camera online state from Dahua**. `cam_enabled` := registration
  `Enable` flag; cameras that disappear from the table are swept offline by
  the existing "no longer reported" logic.
- Response format: `key=value` lines (CRLF), tables indexed `[i]` or keyed
  `uuid:<id>`.

### Uniview (HTTP digest auth, port 80, LAPI — JSON envelopes)

- `GET /LAPI/V1.0/System/DeviceInfo` → `Data.{DeviceName, DeviceModel,
  SerialNumber, FirmwareVersion}`. (Note: `.../BasicInfo` and sibling paths
  599/`ResponseCode 1` — the bare `DeviceInfo` path is the working one.)
- `GET /LAPI/V1.0/Channels/System/ChannelDetailInfos` → `Data.DetailInfos[]`:
  per channel `ID` (1-based), `Name`, `Status` (1 = online; `OffReason`
  carries the offline reason when down), `Manufacturer` (e.g. `HIKVISION`),
  `DeviceModel`, `AddressInfo.{Address, Port, MAC}` (real camera IP+MAC).
- `GET /LAPI/V1.0/Channels/System/DeviceInfos` → `Data.DeviceInfos[]`: per
  channel `ID`, `DeviceModel`, `SerialNumber`, `FirmwareVersion` (merge into
  the detail rows by `ID`).
- Envelope: `Response.{ResponseCode, StatusCode}` — 0 = success; HTTP 599
  with `ResponseCode 1` = path not supported on this firmware. (UNV 404s are
  delivered as truncated chunked responses — treat any read error as "not
  found/offline".)

## Collector — `netbox_sync/collectors/dahua.py`

- `class DahuaSession` — `requests.Session` + `HTTPDigestAuth(DAHUA_USER,
  DAHUA_PASS)`, `verify=False`; `get(path)` → text.
- Parsers (pure, `key=value` text):
  - `_parse_system_info(text)` → `{serial, model (updateSerial), device_type}`
  - `_parse_channel_titles(text)` → `{channel_int: name}`
  - `_parse_remote_devices(text)` → per-slot dicts normalized to
    `{channel, name: None, ip, model, serial, firmware, mac, enable}`; `mac`
    normalized to lowercase colon form, `None` when empty or all-`ff`.
- `probe_dahua(ip, retries=2, retry_delay=3)` → standard probe dict; gated on
  `is_port_open` + `getSystemInfo` serial + `getDeviceClass` == NVR.
- `dahua_collect(ip)` → `{"summary": {name, model, serial, firmware},
  "cameras": [...]}`: merges RemoteDevice rows with ChannelTitle names
  (channel-matched), `online := Enable` (see above), `manufacturer` inferred:
  `Hikvision` when model starts with `DS-2`, else `Dahua`.

## Collector — `netbox_sync/collectors/unv.py`

- `class UnvSession` — digest auth; `get(path)` → envelope `Data`, raising on
  non-0 `ResponseCode` and on transport errors (chunked-truncation == dead
  path/device).
- Parsers (pure, JSON): `_lapi_data(text)` (envelope unwrap/validate),
  `_parse_device_info`, `_parse_channel_details`, `_parse_ipc_device_infos`.
- `probe_unv(ip, ...)` → standard probe dict; gated on port + DeviceInfo
  serial/model.
- `unv_collect(ip)` → same shape as `dahua_collect`; per camera:
  `{channel: ID, name, ip, mac (colon form), online: Status == 1,
  manufacturer: Manufacturer or "Uniview", model, serial, firmware}`.
  Camera MACs feed the existing camera→switch cabling with no extra work.

## NetBox side — `netbox.py`

- Generalize `ensure_camera_device(cam, nvr_name, role_name=None,
  manufacturer="Hikvision")`: new optional `manufacturer` parameter (default
  keeps today's behavior for Hikvision); the camera's device-type manufacturer
  comes from the collector's `manufacturer` field. No custom-field changes —
  the existing `cam_*`/`nvr_*` fields are vendor-neutral.
- New `ensure_dahua_device(probe)` / `ensure_unv_device(probe)` +
  `mark_dahua_offline` / `mark_unv_offline`, mirroring
  `ensure_hikvision_device`/`mark_hikvision_offline` (shared `nvr_*` custom
  fields; role from `DEFAULT_DAHUA_ROLE` / `DEFAULT_UNV_ROLE`, default `NVR`).

## Config / scanner / orchestration

- `config.py`: `DAHUA_USER/PASS/PORT(80)`, `DAHUA_RANGES`,
  `DEFAULT_DAHUA_ROLE=NVR`; `UNV_USER/PASS/PORT(80)`, `UNV_RANGES`,
  `DEFAULT_UNV_ROLE=NVR`; validation: creds required only when the
  corresponding RANGES is set. Env names all-caps (`DAHUA_*`), matching the
  existing convention.
- `scanner.py`: `dahua_nvrs` and `unv_nvrs` blocks after Hikvision, each with
  used-IP exclusion + skipped-count log (the convention the UniFi block now
  follows).
- `sync.py`: per-family processing blocks identical in shape to the Hikvision
  block — `ensure_<vendor>_device`, `nvr_*` custom-field update,
  `ensure_primary_ip`, per-camera `ensure_camera_device(cam, nvr_name,
  manufacturer=cam.get("manufacturer"))`, primary IP, `ensure_camera_interface`
  + `sync_camera_cable` when `cam["mac"]` and `mac_map` (Dahua cameras simply
  won't match), offline sweep by `cf_cam_nvr`. Family offline sweep via the
  existing `_offline_sweep` gate (`nvr_*` fields are shared, so the sweeps use
  the per-family live-IP sets + `mark_<vendor>_offline` on the vendor role).

## Error handling

- All collectors follow the family contract: probe returns None on
  unreachable/unparseable; collect raises, caught per-NVR in sync (ERROR log,
  continue). Per-camera NetBox failures are per-camera WARN/ERROR, never fatal.
- UNV transport weirdness (truncated chunked bodies on unknown paths) is
  contained inside `UnvSession.get` → RuntimeError → collector-level handling.

## Testing

- Parser tests with captured real response snippets: Dahua `key=value` table
  parsing (incl. MAC absent/all-ff → None, 32-slot mapping), UNV envelope
  unwrap + channel detail merge + Status mapping.
- Probe/collect tests with mocked sessions (offline → None; identity fields).
- ensure-camera-device manufacturer parametrization test; NVR ensure/offline
  tests following the Hikvision test style.
- Config validation tests for the new vars.
- Full suite stays green; live run against both NVRs before push.

## Docs / ops

- README: Dahua + UNV subsections (endpoints, fields, the Dahua MAC/online
  caveats), env tables for `DAHUA_*`/`UNV_*` (EN; Farsi half untouched, as
  before).
- `.env.example`: Dahua + UNV credential/range blocks.
- User must add to `.env`: `DAHUA_RANGES=192.168.252.5/32`,
  `UNV_RANGES=192.168.112.66/32` (creds already present).
- Follow-up (not in this spec): Dahua camera MACs/cabling need either
  ONVIF-device MAC reporting or direct camera reachability — out of scope.
