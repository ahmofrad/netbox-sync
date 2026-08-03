# Camera → Switch Cabling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cable each Hikvision camera to the Cisco switch port it is learned on (via switch MAC tables) as a real NetBox `dcim.cable` between the camera's `eth0` interface and the switch interface.

**Architecture:** During the existing Cisco inventory collection, pull `show mac address-table` per switch; build a global `{mac: (switch_ip, port, vid)}` map (skipping inter-switch/uplink ports); after each camera device is ensured in the Hikvision pass, ensure a camera `eth0` interface and reconcile a marker-owned cable to the mapped switch port. Keep-on-absence: cables are never deleted when a MAC vanishes from the tables (aging), only moved on positive evidence.

**Tech Stack:** Python 3, pynetbox, netmiko (existing), pytest with in-memory fakes (existing `FakeEndpoint`/`FakeRecord` in `tests/test_netbox_sync.py`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-03-camera-switch-cabling-design.md`
- Cable ownership marker: `CABLE_MARKER = "netbox-sync:"` (already in `netbox_sync/collectors/cisco.py`); camera cables use description prefix `netbox-sync: mac-table `.
- Camera interface: name `eth0`, type `1000base-t`, description `netbox-sync: camera LAN`.
- Manual (unmarked) cables are never created over, modified, or deleted.
- Keep-on-absence policy: MAC not found in any table → leave existing marked cable untouched (log only).
- Feature is inert unless both `CISCO_RANGES` and `HIKVISION_RANGES` are set; no new env vars.
- Tests run with `py -3 -m pytest` (the `python` command does not exist on this machine; PowerShell, no `&&` chaining).
- Working tree note: `netbox_sync/collectors/hikvision.py` has uncommitted MAC-collection changes — leave them alone; commit only files each task touches.

---

### Task 1: MAC-table parser + collection in the Cisco collector

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (`_parse_mac_table_entry` at :214, `cisco_collect_inventory` at :388)
- Test: `tests/test_cisco_parsers.py`

**Interfaces:**
- Consumes: existing `_parse_mac_table_entry(text)` → `[{vid, mac, port}]`.
- Produces:
  - `_parse_mac_table(text)` → `[{vid: int, mac: "aa:bb:cc:dd:ee:ff", port: str}]`
  - `cisco_collect_inventory(ip)` return dict gains key `"mac_table"` (list, possibly empty). Task 2 consumes this key.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cisco_parsers.py`:

```python
SHOW_MAC_TABLE = """SW1#show mac address-table
          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
   1    0009.0f09.0024    DYNAMIC     Gi1/0/5
  10    b40b.4412.abcd    DYNAMIC     Gi1/0/7
  10    b40b.4412.abcd    STATIC      CPU
Total Mac Addresses for this criterion: 3
"""


def test_parse_mac_table():
    rows = mod._parse_mac_table(SHOW_MAC_TABLE)
    assert rows == [
        {"vid": 1, "mac": "00:09:0f:09:00:24", "port": "Gi1/0/5"},
        {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi1/0/7"},
        {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "CPU"},
    ]


def test_parse_mac_table_empty():
    assert mod._parse_mac_table("SW1#show mac address-table\n") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3 -m pytest tests/test_cisco_parsers.py -k mac_table -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_parse_mac_table'`

- [ ] **Step 3: Implement `_parse_mac_table` and collect the table**

In `netbox_sync/collectors/cisco.py`, directly after `_parse_mac_table_entry` (line 225), add:

```python
def _parse_mac_table(text):
    """Parse full `show mac address-table` output -> [{vid, mac, port}].
    Same row format as the single-address variant; header/footer lines and
    the 'Total Mac Addresses' line never match the row regex."""
    return _parse_mac_table_entry(text)
```

In `cisco_collect_inventory`, directly after the `ip_brief` try/except block (ends line 436), add:

```python
        try:
            mac_table = _parse_mac_table(sess.run("show mac address-table"))
            log("INFO", f"  mac table: {len(mac_table)} entries")
        except Exception as exc:
            mac_table = []
            log("WARN", f"  show mac address-table failed: {exc}")
```

And in the return dict (lines 451-454), add the key:

```python
        return {"summary": summary, "ports": ports,
                "neighbors": neighbors, "inventory": inventory,
                "vlans": vlans, "trunks": trunks, "vtp": vtp,
                "ip_brief": ip_brief, "mac_table": mac_table}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest tests/test_cisco_parsers.py -v`
Expected: all PASS (including the two new tests)

- [ ] **Step 5: Commit**

```powershell
git add netbox_sync/collectors/cisco.py tests/test_cisco_parsers.py
git commit -m "Collect full MAC address table during Cisco inventory"
```

---

### Task 2: `_norm_mac` + `build_mac_map` with uplink guard

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (after `_mac_to_cisco` at :208)
- Test: `tests/test_cisco_parsers.py`

**Interfaces:**
- Consumes: `_short_intf` (:95), `log` from config, Task 1's `"mac_table"` rows.
- Produces:
  - `_norm_mac(mac)` → normalized `"aa:bb:cc:dd:ee:ff"` or `None` (Task 4 uses it).
  - `build_mac_map(collected)` where `collected` is a list of `(probe_dict, dev_id, data_dict)`; returns `{mac: (switch_ip, port, vid)}`. Task 5 passes the real `collected` list from `sync.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cisco_parsers.py`:

```python
def test_norm_mac():
    assert mod._norm_mac("B4:0B:44:12:AB:CD") == "b4:0b:44:12:ab:cd"
    assert mod._norm_mac("b40b.4412.abcd") == "b4:0b:44:12:ab:cd"
    assert mod._norm_mac("not-a-mac") is None
    assert mod._norm_mac(None) is None
    assert mod._norm_mac("") is None


def test_build_mac_map_skips_uplink_ports():
    collected = [
        ({"ip": "10.0.0.1", "hostname": "SW1"}, 7, {
            "neighbors": [{"device_id": "SW2", "platform": "", "ip": None,
                           "local_intf": "GigabitEthernet1/0/1",
                           "remote_intf": "Gi0/1"}],
            "mac_table": [
                {"vid": 1, "mac": "00:09:0f:09:00:24", "port": "Gi1/0/1"},
                {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi1/0/5"},
            ],
        }),
    ]
    m = mod.build_mac_map(collected)
    # Gi1/0/1 is a CDP uplink (long name in neighbors) -> excluded
    assert m == {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/5", 10)}


def test_build_mac_map_first_switch_wins_on_duplicates():
    collected = [
        ({"ip": "10.0.0.1"}, 7, {"neighbors": [], "mac_table": [
            {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi1/0/5"}]}),
        ({"ip": "10.0.0.2"}, 8, {"neighbors": [], "mac_table": [
            {"vid": 10, "mac": "b4:0b:44:12:ab:cd", "port": "Gi2/0/9"}]}),
    ]
    m = mod.build_mac_map(collected)
    assert m["b4:0b:44:12:ab:cd"] == ("10.0.0.1", "Gi1/0/5", 10)


def test_build_mac_map_handles_missing_keys():
    assert mod.build_mac_map([]) == {}
    collected = [({"ip": "10.0.0.1"}, 7, {})]   # no neighbors/mac_table keys
    assert mod.build_mac_map(collected) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest tests/test_cisco_parsers.py -k "norm_mac or build_mac_map" -v`
Expected: FAIL with `AttributeError` for `_norm_mac` / `build_mac_map`

- [ ] **Step 3: Implement**

In `netbox_sync/collectors/cisco.py`, directly after `_mac_to_cisco` (line 212), add:

```python
def _norm_mac(mac):
    """Any common MAC form -> lowercase colon form ('b4:0b:44:12:ab:cd');
    None when the input doesn't hold exactly 12 hex digits."""
    s = re.sub(r'[^0-9a-fA-F]', '', mac or '').lower()
    if len(s) != 12:
        return None
    return ":".join(s[i:i+2] for i in range(0, 12, 2))
```

After `_cisco_mac_lookup` (ends line 574), add:

```python
def build_mac_map(collected):
    """Build {mac: (switch_ip, port, vid)} from all switches' MAC tables.

    `collected` is the Cisco pass list of (probe, dev_id, data). Ports that
    carry a CDP/LLDP neighbor (inter-switch links) are skipped: a MAC seen
    there belongs to a downstream switch, which reports it on a real access
    port. CDP/LLDP uses long interface names, MAC tables short ones — both
    sides are normalized through _short_intf. On duplicate MACs the first
    switch in collection order wins."""
    mac_map = {}
    for probe, _dev_id, data in collected:
        uplinks = {_short_intf(n.get("local_intf"))
                   for n in (data.get("neighbors") or [])}
        for row in (data.get("mac_table") or []):
            port = row.get("port")
            if _short_intf(port) in uplinks:
                continue
            mac = row.get("mac")
            if not mac:
                continue
            if mac in mac_map:
                log("WARN", f"  mac {mac} seen on {mac_map[mac][0]}:"
                            f"{mac_map[mac][1]} and {probe['ip']}:{port}"
                            " — keeping the first")
                continue
            mac_map[mac] = (probe["ip"], port, row.get("vid"))
    return mac_map
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest tests/test_cisco_parsers.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```powershell
git add netbox_sync/collectors/cisco.py tests/test_cisco_parsers.py
git commit -m "Add build_mac_map: MAC to switch-port map with uplink guard"
```

---

### Task 3: `ensure_camera_interface` in netbox.py

**Files:**
- Modify: `netbox_sync/netbox.py` (after `ensure_camera_device` ends at :628)
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: `get_netbox()` (existing).
- Produces:
  - `CAMERA_IFACE_NAME = "eth0"` module constant. Task 4 imports it via `netbox.CAMERA_IFACE_NAME`.
  - `ensure_camera_interface(dev_id, online)` → interface id (int). Task 5 calls it per camera.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_netbox_sync.py` (a new section at the end of the file):

```python
# ── Camera interface + camera→switch cabling ────────────────────────────────

def test_camera_interface_created_when_missing(monkeypatch):
    ifaces = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces))

    iface_id = nbx.ensure_camera_interface(100, online=False)

    assert len(ifaces.created) == 1
    payload = ifaces.created[0]
    assert payload["device"] == 100
    assert payload["name"] == "eth0"
    assert payload["type"] == "1000base-t"
    assert payload["enabled"] is False
    assert payload["description"] == "netbox-sync: camera LAN"
    assert iface_id is not None


def test_camera_interface_refreshes_enabled_only_on_drift(monkeypatch):
    existing = FakeRecord(11, name="eth0", device_id=100, enabled=True)
    ifaces = FakeEndpoint([existing])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces))

    assert nbx.ensure_camera_interface(100, online=True) == 11
    assert ifaces.update_calls == 0          # already in sync -> no write

    assert nbx.ensure_camera_interface(100, online=False) == 11
    assert ifaces.updated == [{"id": 11, "enabled": False}]
    assert ifaces.created == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest tests/test_netbox_sync.py -k camera_interface -v`
Expected: FAIL with `AttributeError: module 'netbox_sync.netbox' has no attribute 'ensure_camera_interface'`

- [ ] **Step 3: Implement**

In `netbox_sync/netbox.py`, directly after `ensure_camera_device` (ends line 628), add:

```python
CAMERA_IFACE_NAME = "eth0"


def ensure_camera_interface(dev_id, online=True):
    """Get-or-create the camera's single LAN interface — the cable
    termination point for camera<->switch cabling. Only `enabled` is
    refreshed on existing interfaces."""
    api = get_netbox()
    existing = api.dcim.interfaces.get(device_id=dev_id, name=CAMERA_IFACE_NAME)
    if existing:
        if bool(getattr(existing, "enabled", True)) != bool(online):
            api.dcim.interfaces.update([{"id": existing.id,
                                         "enabled": bool(online)}])
        return existing.id
    return api.dcim.interfaces.create({
        "device": dev_id, "name": CAMERA_IFACE_NAME, "type": "1000base-t",
        "enabled": bool(online), "mgmt_only": False,
        "description": "netbox-sync: camera LAN"}).id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest tests/test_netbox_sync.py -k camera_interface -v`
Expected: PASS

- [ ] **Step 5: Commit**

```powershell
git add netbox_sync/netbox.py tests/test_netbox_sync.py
git commit -m "Add ensure_camera_interface: camera eth0 as cable endpoint"
```

---

### Task 4: `sync_camera_cable` reconciliation in cisco.py

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (after `sync_cdp_cables`, end of file)
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: `CABLE_MARKER` (:746), `_cable_iface_ids` (:748), `_norm_mac` (Task 2), `netbox.CAMERA_IFACE_NAME` (Task 3), `build_mac_map`'s output shape `{mac: (switch_ip, port, vid)}`.
- Produces:
  - `sync_camera_cable(cam_dev_id, cam_name, cam_iface_id, mac, mac_map, switch_by_ip)` → `None`. `switch_by_ip` is `{switch_mgmt_ip: {"dev_id": int, "name": str}}`. Task 5 builds both arguments.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_netbox_sync.py`, after the Task 3 tests:

```python
_CAM_IFACE = FakeRecord(11, name="eth0", device_id=100)
_SW_IFACE = FakeRecord(55, name="Gi1/0/5", device_id=7)
_SW_IFACE2 = FakeRecord(77, name="Gi1/0/9", device_id=7)
_CAM_MAC_MAP = {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/5", 10)}
_CAM_SWITCHES = {"10.0.0.1": {"dev_id": 7, "name": "SW1"}}


def _cam_cable_api(sw_ifaces, cables):
    return _fake_api(interfaces=FakeEndpoint([_CAM_IFACE] + sw_ifaces),
                     cables=FakeEndpoint(cables))


def test_camera_cable_created(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cam_cable_api([_SW_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert len(api.dcim.cables.created) == 1
    payload = api.dcim.cables.created[0]
    assert payload["a_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 11}]
    assert payload["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 55}]
    assert payload["description"] == "netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5"


def test_camera_cable_refreshed_when_unchanged(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert {u["id"] for u in api.dcim.cables.updated} == {9}
    # refresh only — terminations untouched
    assert "a_terminations" not in api.dcim.cables.updated[0]


def test_camera_cable_moved_when_mac_found_elsewhere(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE, _SW_IFACE2], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)
    moved_map = {"b4:0b:44:12:ab:cd": ("10.0.0.1", "Gi1/0/9", 10)}

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            moved_map, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert len(api.dcim.cables.updated) == 1
    upd = api.dcim.cables.updated[0]
    assert upd["id"] == 9
    assert upd["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 77}]


def test_camera_cable_kept_when_mac_absent(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=100,
                        description="netbox-sync: mac-table eth0 <-> SW1 Gi1/0/5",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            {}, _CAM_SWITCHES)   # empty map: aged out

    assert api.dcim.cables.created == []
    assert api.dcim.cables.updated == []
    assert api.dcim.cables.deleted_ids == []    # keep-on-absence


def test_camera_cable_never_overrides_manual_cable(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    manual = FakeRecord(8, device_id=100, description="manual doc",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cam_cable_api([_SW_IFACE], [manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
    assert api.dcim.cables.updated == []


def test_camera_cable_skips_when_switch_iface_missing(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cam_cable_api([], [])   # switch interface not in NetBox
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_camera_cable(100, "CAM1", 11, "b4:0b:44:12:ab:cd",
                            _CAM_MAC_MAP, _CAM_SWITCHES)

    assert api.dcim.cables.created == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m pytest tests/test_netbox_sync.py -k camera_cable -v`
Expected: FAIL with `AttributeError: ... no attribute 'sync_camera_cable'`

- [ ] **Step 3: Implement**

In `netbox_sync/collectors/cisco.py`, append at the end of the file:

```python
def sync_camera_cable(cam_dev_id, cam_name, cam_iface_id, mac, mac_map,
                      switch_by_ip):
    """Reconcile one camera<->switch cable from the MAC-table map.

    Keep-on-absence: when the camera MAC is in no switch table this run
    (aged out / idle camera), existing marked cables are left untouched —
    a cable is only moved on positive evidence of a new port. Manual
    (unmarked) cables are never modified or created over."""
    api = netbox.get_netbox()
    mac = _norm_mac(mac)
    if not mac:
        return
    hit = mac_map.get(mac)
    if not hit:
        log("DEBUG", f"  camera {cam_name}: mac {mac} not in any switch "
                     "table — keeping existing cable")
        return
    sw_ip, port, _vid = hit
    sw = (switch_by_ip or {}).get(sw_ip)
    if not sw:
        log("WARN", f"  camera {cam_name}: switch {sw_ip} has no NetBox "
                    "device this run — skipping cable")
        return
    sw_iface = api.dcim.interfaces.get(device_id=sw["dev_id"], name=port)
    if not sw_iface:
        log("WARN", f"  camera {cam_name}: iface {port} not found on "
                    f"{sw['name']} — skipping cable")
        return

    desc = (f"{CABLE_MARKER} mac-table {netbox.CAMERA_IFACE_NAME} <-> "
            f"{sw['name']} {port}")
    term_a = [{"object_type": "dcim.interface", "object_id": cam_iface_id}]
    term_b = [{"object_type": "dcim.interface", "object_id": sw_iface.id}]

    cables = list(api.dcim.cables.filter(device_id=cam_dev_id))
    marked = [c for c in cables
              if (c.description or "").startswith(CABLE_MARKER)]
    unmarked = [c for c in cables
                if not (c.description or "").startswith(CABLE_MARKER)]
    mine = next((c for c in marked
                 if cam_iface_id in set(_cable_iface_ids(c))), None)

    if mine:
        if set(_cable_iface_ids(mine)) == {cam_iface_id, sw_iface.id}:
            api.dcim.cables.update([{"id": mine.id, "description": desc}])
            return
        api.dcim.cables.update([{"id": mine.id,
                                 "a_terminations": term_a,
                                 "b_terminations": term_b,
                                 "description": desc}])
        log("INFO", f"  camera {cam_name}: cable moved to "
                    f"{sw['name']} {port}")
        return

    if any(cam_iface_id in set(_cable_iface_ids(c))
           or sw_iface.id in set(_cable_iface_ids(c)) for c in unmarked):
        log("DEBUG", f"  camera {cam_name}: manual cable present on "
                     f"{netbox.CAMERA_IFACE_NAME} or {port}, leaving untouched")
        return
    try:
        api.dcim.cables.create({"a_terminations": term_a,
                                "b_terminations": term_b,
                                "description": desc})
        log("INFO", f"  camera {cam_name}: cabled "
                    f"{netbox.CAMERA_IFACE_NAME} <-> {sw['name']} {port}")
    except Exception as exc:
        log("WARN", f"  camera {cam_name}: cable create failed: {exc}")
```

Note: if the switch port already carries a cable to another device (e.g. a
stale CDP cable), NetBox rejects the create — the `except` logs WARN and the
run continues. This is intentional.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m pytest tests/test_netbox_sync.py -k camera -v`
Expected: all PASS (camera_interface + camera_cable tests)

- [ ] **Step 5: Commit**

```powershell
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add sync_camera_cable: camera-to-switch cable reconciliation"
```

---

### Task 5: Wire it up in sync.py

**Files:**
- Modify: `netbox_sync/sync.py` (imports :4-19 and :40-53; after Cisco pass-2 loop ending :396; Hikvision camera loop :744-757)

**Interfaces:**
- Consumes: `build_mac_map` (Task 2), `sync_camera_cable` (Task 4), `ensure_camera_interface` (Task 3), Task 1's `"mac_table"` key.
- Produces: `mac_map` and `switch_by_ip` locals in `run_sync`, used by the Hikvision camera loop.

- [ ] **Step 1: Add imports**

In the `from netbox_sync.collectors.cisco import (...)` block (lines 4-19), add `build_mac_map` and `sync_camera_cable` to the list (keep alphabetical-ish order as-is):

```python
from netbox_sync.collectors.cisco import (cisco_collect_inventory,
                                          sync_cisco_interfaces,
                                          ensure_vlan_group,
                                          sync_cisco_vlans,
                                          sync_interface_vlans,
                                          ensure_svi_interface,
                                          sweep_stale_vlans,
                                          sweep_legacy_site_vlans,
                                          sync_cdp_cables,
                                          sync_camera_cable,
                                          build_mac_map,
                                          _site_vlan_index,
                                          _mac_to_cisco,
                                          _cisco_mac_lookup,
                                          _norm_sw_name,
                                          _broadcast_components,
                                          _component_key,
                                          _sweep_stale_groups)
```

In the `from netbox_sync.netbox import (...)` block (lines 40-53), add `ensure_camera_interface` right after `ensure_camera_device`:

```python
                                ensure_hikvision_device, ensure_camera_device,
                                ensure_camera_interface,
```

- [ ] **Step 2: Build the maps after the Cisco pass**

Insert between line 396 (end of the Cisco pass-2 loop) and line 398 (`# ── Process FortiGates`):

```python
    # MAC -> switch-port map for camera cabling. Empty when no Cisco
    # switches were scanned (family disabled) — cabling then no-ops.
    mac_map = build_mac_map(collected)
    switch_by_ip = {p["ip"]: {"dev_id": d, "name": p.get("hostname") or p["ip"]}
                    for p, d, _ in collected}
    if mac_map:
        log("INFO", f"  camera cabling: MAC map holds {len(mac_map)} entries "
                    f"from {len(collected)} switch(es)")
```

- [ ] **Step 3: Call from the camera loop**

In the Hikvision camera loop, replace lines 745-757:

```python
        for cam in data["cameras"]:
            try:
                cam_dev = ensure_camera_device(cam, nvr_name)
                serial = (cam.get("serial") or "").strip()
                if serial:
                    seen_camera_serials.add(serial)
                if cam.get("ip"):
                    try:
                        ensure_primary_ip(cam_dev, cam["ip"], cam.get("name"))
                    except Exception as e:
                        log("WARN", f"  camera {cam.get('name')} primary IP failed: {e}")
            except Exception as e:
                log("ERROR", f"  camera sync failed for ch{cam.get('channel')}: {e}")
```

with:

```python
        for cam in data["cameras"]:
            try:
                cam_dev = ensure_camera_device(cam, nvr_name)
                serial = (cam.get("serial") or "").strip()
                if serial:
                    seen_camera_serials.add(serial)
                if cam.get("ip"):
                    try:
                        ensure_primary_ip(cam_dev, cam["ip"], cam.get("name"))
                    except Exception as e:
                        log("WARN", f"  camera {cam.get('name')} primary IP failed: {e}")
                try:
                    cam_iface = ensure_camera_interface(
                        cam_dev, bool(cam.get("online")))
                except Exception as e:
                    cam_iface = None
                    log("WARN", f"  camera {cam.get('name')} interface sync failed: {e}")
                if cam_iface and cam.get("mac") and mac_map:
                    try:
                        sync_camera_cable(cam_dev, cam.get("name"), cam_iface,
                                          cam["mac"], mac_map, switch_by_ip)
                    except Exception as e:
                        log("WARN", f"  camera {cam.get('name')} cable sync failed: {e}")
            except Exception as e:
                log("ERROR", f"  camera sync failed for ch{cam.get('channel')}: {e}")
```

- [ ] **Step 4: Run the full test suite**

Run: `py -3 -m pytest tests/ -q`
Expected: all PASS (no regressions; `sync.py` isn't unit-tested directly — the wiring is verified by the import succeeding and by the manual sync run later)

Also verify the module imports cleanly:

Run: `py -3 -c "import netbox_sync.sync"`
Expected: no output, exit code 0

- [ ] **Step 5: Commit**

```powershell
git add netbox_sync/sync.py
git commit -m "Wire camera cabling: MAC map build + per-camera cable sync"
```

---

### Task 6: Docs (README + .env.example) and push

**Files:**
- Modify: `README.md` (Hikvision section)
- Modify: `.env.example` (Cisco block)

- [ ] **Step 1: README**

In `README.md`, locate the Hikvision section (added in commit 4ea6281). Append a subsection:

```markdown
### Camera → switch cabling

When the Cisco family is also enabled (`CISCO_RANGES` set and reachable),
each camera with a known MAC is cabled in NetBox to the switch port it is
learned on: one `eth0` interface per camera and a real cable between it and
the switch interface (description `netbox-sync: mac-table ...`). Cables are
managed like CDP cables — only marker-owned ones are touched. Because
switch MAC tables age out idle entries (~5 min), a cable is never deleted
when a camera's MAC is momentarily missing; it is only moved when the MAC
is positively found on a different port. With Cisco disabled, cabling is
silently skipped.
```

(Translate/keep style consistent with the surrounding Hikvision section; the
README has an English part and a Farsi part — add to the English part only,
as before.)

- [ ] **Step 2: .env.example**

In `.env.example`, in the Cisco block directly above the `CISCO_RANGES` line, add:

```
# Also enables camera->switch cabling (MAC-table lookup) when HIKVISION_RANGES is set
```

- [ ] **Step 3: Verify nothing broke**

Run: `py -3 -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 4: Commit and push**

```powershell
git add README.md .env.example
git commit -m "Document camera-to-switch cabling (README, .env.example)"
git push origin main
```

---

## Self-Review Notes

- **Spec coverage:** §1 MAC-table collection → Task 1. §2 map + uplink guard → Task 2 (+ wiring in Task 5 Step 2). §3 camera interface → Task 3. §4 cable reconciliation incl. keep-on-absence / move / manual-cable rules → Task 4. §5 orchestration → Task 5. §6 tests → per-task test steps. §7 docs → Task 6.
- **Type consistency:** `mac_map` values are `(switch_ip, port, vid)` tuples everywhere; `switch_by_ip` values are `{"dev_id", "name"}` dicts everywhere; `ensure_camera_interface` returns an id used as `cam_iface_id` in `sync_camera_cable(cam_dev_id, cam_name, cam_iface_id, mac, mac_map, switch_by_ip)` — matches in Tasks 4 and 5.
- **Out of scope (from spec, deliberate):** no VLAN assignment on camera interfaces; no `cam_switch`/`cam_switch_port` custom fields (superseded by real cables).
