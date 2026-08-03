# Camera → Switch Cabling from MAC Tables — Design Spec

Date: 2026-08-03

## Goal

For each Hikvision camera synced by the Hikvision family, identify the Cisco
switch port the camera is plugged into (via the switches' MAC address tables)
and record it in NetBox as a **real cable** between a camera interface and the
switch interface. Not custom fields — actual `dcim.cable` records, following
the existing `CABLE_MARKER` ownership convention used by CDP/LLDP cabling.

Prerequisite: both families enabled (`HIKVISION_RANGES` and `CISCO_RANGES`).
With Cisco disabled the feature is silently skipped.

## Approach

Full-table pull (chosen over per-camera targeted lookups): each switch's whole
MAC table is fetched with one extra SSH command during the existing Cisco
inventory collection; a global MAC→port map is built in `sync.py`; every
camera lookup is a dict hit. The alternative (per-camera `show mac
address-table address <mac>` across all switches) costs 30 cameras × N
switches SSH commands per run.

## Components

### 1. MAC table collection — `netbox_sync/collectors/cisco.py`

- New pure parser `_parse_mac_table(text)` → `[{vid, mac, port}]`. Reuses the
  row regex from `_parse_mac_table_entry` (MAC normalized to lowercase colon
  form). `_parse_mac_table_entry` is kept for the existing FortiGate use.
- `cisco_collect_inventory` additionally runs `show mac address-table` and
  returns the rows under a new `"mac_table"` key. Failure logs a warning and
  yields `[]` — cabling is skipped, the rest of the sync is unaffected.

### 2. Global MAC map — `netbox_sync/sync.py` (Cisco pass)

- After the Cisco collection pass, build
  `mac_map = {mac: (switch_ip, port, vid)}` from all switches' `mac_table`
  data, keyed by normalized MAC.
- **Uplink guard:** entries learned on a port that appears in that switch's
  CDP/LLDP neighbor list (inter-switch link) are skipped — a camera MAC seen
  on an uplink belongs to a downstream switch, which reports it on a real
  access port.
- If the same MAC is reported on several access ports (shouldn't happen for
  cameras), the first switch in collection order wins and a warning is logged.

### 3. Camera interface — `netbox_sync/netbox.py`

- New `ensure_camera_interface(dev_id, online)` → interface id.
- Cameras currently have no interfaces; one is created/updated per camera:
  - `name`: `eth0`, `type`: `1000base-t`, `enabled`: camera online status,
    `description`: `netbox-sync: camera LAN`, `mgmt_only`: False.
- Idempotent get-or-create by (device, name); only `enabled` is refreshed on
  updates.

### 4. Cable reconciliation — `netbox_sync/collectors/cisco.py`

- New `sync_camera_cable(cam_dev_id, cam_dev_name, cam_iface_id, mac, mac_map,
  switch_dev_by_ip)`:
  - MAC absent from `mac_map` → keep any existing marked cable, log, return.
    (MAC tables age out idle entries in ~5 min; deleting on absence would
    flap. Cables are only ever *moved* on positive evidence.)
  - MAC found → resolve switch device (by management IP from the Cisco pass)
    and switch interface by port name. If either is missing in NetBox, log and
    skip.
  - Reuses `CABLE_MARKER = "netbox-sync:"`; description:
    `netbox-sync: mac-table eth0 <-> SW-NAME Gi1/0/5`.
  - Existing marked cable on the same endpoints → refresh description.
  - Existing marked cable on *different* endpoints → update terminations
    (move) to the newly found switch port.
  - Manual (unmarked) cables on either interface → never touched, log DEBUG.

### 5. Orchestration — `netbox_sync/sync.py` (Hikvision camera loop)

The Cisco block already runs before the Hikvision block in `run_sync`, so
`mac_map`, switch devices and switch interfaces all exist when cameras are
processed. After `ensure_camera_device` per camera:

1. `cam_iface_id = ensure_camera_interface(cam_dev, cam["online"])`
2. If `cam["mac"]` and `mac_map` non-empty → `sync_camera_cable(...)`.

A `switch_dev_by_ip` map (management IP → NetBox device id) is built during
the Cisco pass alongside `mac_map`. Cameras without a MAC, or when Cisco is
disabled, skip cabling silently.

## Error handling

- MAC table fetch failure per switch: WARN, that switch contributes nothing.
- Switch/interface resolution failure: WARN/DEBUG, camera skipped.
- Cable create/update failure: WARN, sync continues.
- Nothing in this feature can fail the rest of the run (all calls wrapped in
  the camera loop's existing try/except).

## Testing

Following the existing `tests/` style (pytest, mocked NetBox API):

- `_parse_mac_table`: sample `show mac address-table` output → rows,
  MAC normalization.
- Map building with uplink guard: MAC on a CDP-neighbor port is excluded.
- `sync_camera_cable` with a mocked API: create new cable, refresh unchanged,
  move on different port, keep-on-absence, manual cable untouched.
- `ensure_camera_interface`: get-or-create + enabled refresh.

## Docs

- README: new "Camera → switch cabling" subsection (requires both families;
  behavior incl. keep-on-absence policy).
- `.env.example`: comment that camera cabling activates when `CISCO_RANGES`
  is set. No new env vars.
