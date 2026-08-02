# Cisco VLAN Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync VLANs from Cisco switches into NetBox IPAM (per-site), wire interface VLAN linkage, and sweep stale marker-owned VLANs per site.

**Architecture:** Two new commands in the Cisco collector (`show vlan brief`, `show interfaces trunk`), three new sync functions in `collectors/cisco.py`, wiring in `sync.py`. Marker ownership (`netbox-sync:`) — manual VLANs never modified or deleted.

**Tech Stack:** Python 3.9+, netmiko, pynetbox, pytest fake harness.

**Spec:** `docs/superpowers/specs/2026-07-29-cisco-vlans-design.md`

## Global Constraints

- Per-site VLANs: lookup/create by `(vid, site_id)`; marker prefix `netbox-sync:`.
- Unmarked (manual) VLANs: never updated or deleted; their IDs still used for linkage.
- Default-all trunk ranges (`1-4094`, `all`, empty) → `mode="tagged-all"`, never an explicit list.
- Failures WARN-and-continue per switch; sweep errors ERROR-logged.
- Tests: `.\.venv\Scripts\python.exe -m pytest tests\ -q` (87 passing at plan time).

---

### Task 1: VLAN/trunk parsers

**Files:**
- Modify: `netbox_sync/collectors/cisco.py`
- Test: `tests/test_cisco_parsers.py`

**Interfaces:**
- Produces: `_parse_vlan_brief(text) -> [{"vid": int, "name": str, "status": str}]`; `_parse_interfaces_trunk(text) -> [{"port","mode","native","allowed","active"}]`; `_expand_vlan_list(spec) -> set[int] | None` (None = default-all).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cisco_parsers.py`)

```python
VLAN_BRIEF = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/3, Gi1/0/4
10   USERS                            active    Gi1/0/2
20   SERVER VLAN                      active
100  fddi-default                     act/unsup
"""


def test_parse_vlan_brief():
    vlans = mod._parse_vlan_brief(VLAN_BRIEF)
    assert vlans == [
        {"vid": 1, "name": "default", "status": "active"},
        {"vid": 10, "name": "USERS", "status": "active"},
        {"vid": 20, "name": "SERVER VLAN", "status": "active"},
        {"vid": 100, "name": "fddi-default", "status": "act/unsup"},
    ]


INTERFACES_TRUNK = """Port        Mode             Encapsulation  Status        Native vlan
Gi1/0/1     on               802.1q         trunking      1
Te1/1/1     on               802.1q         trunking      10

Port        Vlans allowed on trunk
Gi1/0/1     1-4094
Te1/1/1     1,10,20

Port        Vlans allowed and active in management domain
Gi1/0/1     1,10
Te1/1/1     10,20-22

Port        Vlans in spanning tree forwarding state and not pruned
Gi1/0/1     1,10
"""


def test_parse_interfaces_trunk():
    trunks = {t["port"]: t for t in mod._parse_interfaces_trunk(INTERFACES_TRUNK)}
    assert trunks["Gi1/0/1"]["native"] == 1
    assert trunks["Gi1/0/1"]["allowed"] == "1-4094"
    assert trunks["Gi1/0/1"]["active"] == "1,10"
    assert trunks["Te1/1/1"]["native"] == 10
    assert trunks["Te1/1/1"]["active"] == "10,20-22"


def test_expand_vlan_list():
    assert mod._expand_vlan_list("1,10,20-22") == {1, 10, 20, 21, 22}
    assert mod._expand_vlan_list("1-4094") is None
    assert mod._expand_vlan_list("all") is None
    assert mod._expand_vlan_list("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cisco_parsers.py -q`
Expected: 3 FAIL — `AttributeError` (functions don't exist).

- [ ] **Step 3: Implement** (append to the parsers section of `netbox_sync/collectors/cisco.py`, after `_parse_lldp_detail`)

```python
def _parse_vlan_brief(text):
    """Parse `show vlan brief`: vid, name, status. Ports column ignored."""
    vlans = []
    for line in text.splitlines():
        s = line.rstrip()
        m = re.match(r'^(\d+)\s+(.+?)\s+(active|act/unsup|suspended|shutdown)\b',
                     s, re.IGNORECASE)
        if not m: continue
        vlans.append({"vid": int(m.group(1)), "name": m.group(2).strip(),
                      "status": m.group(3).lower()})
    return vlans

def _expand_vlan_list(spec):
    """Expand "1,10,20-25,100" into a set of vids. Returns None for the
    default all-VLANs range — the caller maps that to mode 'tagged-all'
    instead of an explicit tagged list."""
    s = (spec or "").strip().lower()
    if not s or s in ("all", "1-4094", "1-4096"):
        return None
    vids = set()
    for part in s.split(","):
        part = part.strip()
        if not part: continue
        m = re.match(r'^(\d+)(?:-(\d+))?$', part)
        if not m: continue
        lo = int(m.group(1)); hi = int(m.group(2) or lo)
        vids.update(range(lo, min(hi, 4094) + 1))
    return vids

def _parse_interfaces_trunk(text):
    """Parse `show interfaces trunk` into per-port dicts. Tracks the
    sectioned tables: main (mode/native), 'Vlans allowed on trunk',
    'Vlans allowed and active in management domain'."""
    trunks = {}
    section = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip(): continue
        low = s.lower()
        if "vlans allowed on trunk" in low:
            section = "allowed"; continue
        if "vlans allowed and active" in low:
            section = "active"; continue
        if "vlans in spanning tree" in low:
            section = None; continue
        if re.match(r'^Port\s+Mode\s+Encapsulation', s, re.IGNORECASE):
            section = "main"; continue
        m = re.match(r'^(\S+)\s+(on|desirable|auto|trunk|off|nonegotiate)\s+'
                     r'(\S+)\s+(\S+)\s+(\d+)\s*$', s, re.IGNORECASE)
        if m and section in (None, "main"):
            trunks[m.group(1)] = {"port": m.group(1), "mode": m.group(2).lower(),
                                  "native": int(m.group(5)),
                                  "allowed": None, "active": None}
            continue
        m2 = re.match(r'^(\S+)\s+([\d,\-]+)\s*$', s)
        if m2 and section in ("allowed", "active") and m2.group(1) in trunks:
            trunks[m2.group(1)][section] = m2.group(2)
    return list(trunks.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cisco_parsers.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_cisco_parsers.py
git commit -m "Add VLAN brief and trunk parsers for Cisco"
```

---

### Task 2: `sync_cisco_vlans`

**Files:**
- Modify: `netbox_sync/collectors/cisco.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Consumes: Task 1 parsers' output shape.
- Produces: `VLAN_MARKER = "netbox-sync:"`; `sync_cisco_vlans(site_id: int, hostname: str, vlans: list[dict]) -> dict[int, int]` ({vid: netbox_id}).

- [ ] **Step 1: Write the failing test** (append to `tests/test_netbox_sync.py`)

```python
# ── Cisco VLAN sync ──────────────────────────────────────────────────────────

def _vlan_api(vlan_items):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(vlans=FakeEndpoint(vlan_items)))


def test_sync_cisco_vlans_create_update_and_manual_reuse(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(50, vid=10, description="netbox-sync: last seen OLD")
    manual = FakeRecord(51, vid=20, description="manual vlan")
    api = _vlan_api([marked, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    vid_map = cisco.sync_cisco_vlans(3, "SW1", [
        {"vid": 10, "name": "USERS", "status": "active"},
        {"vid": 20, "name": "SERVERS", "status": "active"},
        {"vid": 30, "name": "GUEST", "status": "active"},
    ])

    assert vid_map == {10: 50, 20: 51, 30: vid_map[30]}
    # marked existing -> updated; manual -> untouched; new -> created with site
    assert {u["id"] for u in api.ipam.vlans.updated} == {50}
    assert len(api.ipam.vlans.created) == 1
    assert api.ipam.vlans.created[0]["vid"] == 30
    assert api.ipam.vlans.created[0]["site"] == 3
    assert api.ipam.vlans.created[0]["description"].startswith(cisco.VLAN_MARKER)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py::test_sync_cisco_vlans_create_update_and_manual_reuse -q`
Expected: FAIL — `AttributeError: ... no attribute 'sync_cisco_vlans'`.

- [ ] **Step 3: Implement** (append to `netbox_sync/collectors/cisco.py`, before the cable section)

```python
# ── VLANs ────────────────────────────────────────────────────────────────────

# Ownership marker: only VLANs whose description starts with this prefix are
# updated/deleted by the sync. Manual VLANs are never modified.
VLAN_MARKER = "netbox-sync:"

def sync_cisco_vlans(site_id, hostname, vlans):
    """Get-or-create each VLAN in IPAM for the site; refresh marker-owned
    records. Returns {vid: netbox_id} for interface linkage."""
    api = netbox.get_netbox()
    vid_map = {}
    for v in vlans:
        vid = v["vid"]
        payload = {"vid": vid, "name": v.get("name") or f"VLAN{vid:04d}",
                   "status": "active",
                   "description": f"{VLAN_MARKER} last seen {hostname}"}
        existing = api.ipam.vlans.get(vid=vid, site_id=site_id)
        if existing:
            if (existing.description or "").startswith(VLAN_MARKER):
                api.ipam.vlans.update([{"id": existing.id, **payload}])
            vid_map[vid] = existing.id
            continue
        try:
            vid_map[vid] = api.ipam.vlans.create({**payload, "site": site_id}).id
        except Exception as exc:
            log("WARN", f"  vlan {vid}: create failed on {hostname}: {exc}")
    return vid_map
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (91).

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add per-site VLAN sync with marker ownership"
```

---

### Task 3: `sync_interface_vlans` + `sweep_stale_vlans`

**Files:**
- Modify: `netbox_sync/collectors/cisco.py`
- Test: `tests/test_netbox_sync.py`

**Interfaces:**
- Produces: `sync_interface_vlans(dev_id: int, ports: list[dict], trunks: list[dict], vid_map: dict[int, int]) -> None`; `sweep_stale_vlans(site_id: int, seen_vids: set[int]) -> None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_netbox_sync.py`)

```python
def test_sync_interface_vlans_access_trunk_and_tagged_all(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="Gi1/0/1", device_id=7),
        FakeRecord(2, name="Gi1/0/2", device_id=7),
        FakeRecord(3, name="Gi1/0/3", device_id=7),
        FakeRecord(4, name="Gi1/0/4", device_id=7),
    ])
    api = SimpleNamespace(
        dcim=SimpleNamespace(interfaces=ifaces_ep),
        ipam=SimpleNamespace(vlans=FakeEndpoint()))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"port": "Gi1/0/1", "name": "", "status": "connected", "vlan": "10",
         "duplex": "full", "speed": "1000", "type": "10/100/1000BaseTX"},
        {"port": "Gi1/0/2", "name": "", "status": "connected", "vlan": "trunk",
         "duplex": "full", "speed": "1000", "type": "1000BaseSX SFP"},
        {"port": "Gi1/0/3", "name": "", "status": "connected", "vlan": "trunk",
         "duplex": "full", "speed": "10G", "type": "SFP-10GBase-SR"},
        {"port": "Gi1/0/4", "name": "", "status": "connected", "vlan": "routed",
         "duplex": "full", "speed": "1000", "type": "10/100/1000BaseTX"},
    ]
    trunks = [
        {"port": "Gi1/0/2", "mode": "on", "native": 1,
         "allowed": "1-4094", "active": "1-4094"},
        {"port": "Gi1/0/3", "mode": "on", "native": 10,
         "allowed": "1,10,20-22", "active": "10,20-22"},
    ]
    vid_map = {1: 101, 10: 110, 20: 120, 21: 121, 22: 122, 99: 199}
    cisco.sync_interface_vlans(7, ports, trunks, vid_map)

    by_id = {u["id"]: u for u in ifaces_ep.updated}
    assert by_id[1]["mode"] == "access" and by_id[1]["untagged_vlan"] == 110
    assert by_id[2]["mode"] == "tagged-all"      # 1-4094 -> no explicit list
    assert by_id[2]["untagged_vlan"] == 101
    assert by_id[3]["mode"] == "tagged"
    assert by_id[3]["untagged_vlan"] == 110
    assert by_id[3]["tagged_vlans"] == [110, 120, 121, 122]
    assert 4 not in by_id                        # routed -> untouched


def test_sweep_stale_vlans(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    seen = FakeRecord(50, vid=10, description="netbox-sync: last seen SW1")
    stale = FakeRecord(51, vid=20, description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, vid=30, description="manual vlan")
    api = _vlan_api([seen, stale, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sweep_stale_vlans(3, {10, 40})

    assert api.ipam.vlans.deleted_ids == [51]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_netbox_sync.py -k "interface_vlans or sweep_stale" -q`
Expected: FAIL — `AttributeError` (functions don't exist).

- [ ] **Step 3: Implement** (append after `sync_cisco_vlans` in `netbox_sync/collectors/cisco.py`)

```python
def sync_interface_vlans(dev_id, ports, trunks, vid_map):
    """Wire VLAN linkage on switch interfaces: access untagged, trunk
    native + tagged (or tagged-all for the default range)."""
    api = netbox.get_netbox()
    by_name = {str(i.name): i
               for i in api.dcim.interfaces.filter(device_id=dev_id)}
    trunk_by_port = {t["port"]: t for t in trunks}
    for p in ports:
        iface = by_name.get(p["port"])
        if not iface: continue
        vlan_col = (p.get("vlan") or "").strip().lower()
        if vlan_col == "routed": continue
        t = trunk_by_port.get(p["port"])
        payload = None
        if t or vlan_col == "trunk":
            payload = {"id": iface.id}
            native = (t or {}).get("native")
            if native in vid_map:
                payload["untagged_vlan"] = vid_map[native]
            expanded = _expand_vlan_list((t or {}).get("active")
                                         or (t or {}).get("allowed"))
            if expanded is None:
                payload["mode"] = "tagged-all"
            else:
                payload["mode"] = "tagged"
                payload["tagged_vlans"] = [vid_map[v] for v in sorted(expanded)
                                           if v in vid_map]
        elif vlan_col.isdigit() and int(vlan_col) in vid_map:
            payload = {"id": iface.id, "mode": "access",
                       "untagged_vlan": vid_map[int(vlan_col)]}
        if payload:
            api.dcim.interfaces.update([payload])

def sweep_stale_vlans(site_id, seen_vids):
    """Delete marker-owned VLANs at the site that no processed switch
    reported this run. Manual (unmarked) VLANs are never touched."""
    api = netbox.get_netbox()
    for vlan in list(api.ipam.vlans.filter(site_id=site_id)):
        if not (vlan.description or "").startswith(VLAN_MARKER):
            continue
        if vlan.vid not in seen_vids:
            try:
                vlan.delete()
                log("INFO", f"  vlan {vlan.vid} (site {site_id}) deleted — no longer seen")
            except Exception as exc:
                log("WARN", f"  could not delete stale vlan {vlan.vid}: {exc}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ -q`
Expected: all pass (93).

- [ ] **Step 5: Commit**

```bash
git add netbox_sync/collectors/cisco.py tests/test_netbox_sync.py
git commit -m "Add interface VLAN linkage and per-site stale VLAN sweep"
```

---

### Task 4: Collect + wire + docs + release

**Files:**
- Modify: `netbox_sync/collectors/cisco.py` (`cisco_collect_inventory`)
- Modify: `netbox_sync/sync.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1–3. `cisco_collect_inventory` return gains `"vlans"` and `"trunks"` keys.

- [ ] **Step 1: Collect the two new commands** (in `cisco_collect_inventory`, after the neighbors block)

```python
        try:
            vlans = _parse_vlan_brief(sess.run("show vlan brief"))
            log("INFO", f"  vlans: {len(vlans)}")
        except Exception as exc:
            vlans = []
            log("WARN", f"  show vlan brief failed: {exc}")
        try:
            trunks = _parse_interfaces_trunk(sess.run("show interfaces trunk"))
            log("INFO", f"  trunks: {len(trunks)}")
        except Exception as exc:
            trunks = []
            log("WARN", f"  show interfaces trunk failed: {exc}")
```

and extend the return dict with `"vlans": vlans, "trunks": trunks`.

- [ ] **Step 2: Wire into `run_sync`** (in `netbox_sync/sync.py` Cisco block)

Extend the netbox import with the three new functions, unpack `vlans`/`trunks`
from `data`, then replace the interface/inventory/cable sequence with:

```python
        site_id = None
        try:
            dev_rec = api.dcim.devices.get(id=dev_id)
            site_id = getattr(getattr(dev_rec, "site", None), "id", None)
        except Exception:
            site_id = None

        vid_map = {}
        if site_id:
            try:
                vid_map = sync_cisco_vlans(site_id, probe.get("hostname") or "", vlans)
                site_vlan_seen.setdefault(site_id, set()).update(vid_map.keys())
            except Exception as e:
                log("WARN", f"  VLAN sync failed for {ip}: {e}")
        else:
            log("WARN", f"  no site on device for {ip} — skipping VLAN sync")

        try:
            sync_cisco_interfaces(dev_id, ports)
            log("INFO", f"  [OK] Cisco {ip} — {len(ports)} interfaces synced")
        except Exception as e:
            log("ERROR", f"  Cisco interface sync failed for {ip}: {e}")

        if vid_map:
            try:
                sync_interface_vlans(dev_id, ports, trunks, vid_map)
                log("INFO", f"  [OK] Cisco {ip} — VLAN linkage synced")
            except Exception as e:
                log("ERROR", f"  Cisco VLAN linkage failed for {ip}: {e}")
```

Initialize `site_vlan_seen = {}` before the Cisco loop, and after it add:

```python
    for site_id, seen in site_vlan_seen.items():
        try:
            sweep_stale_vlans(site_id, seen)
        except Exception as e:
            log("ERROR", f"  VLAN sweep failed for site {site_id}: {e}")
```

- [ ] **Step 3: README (EN)** — in the Cisco bullet of "Supported hardware", extend the commands list with `show vlan brief`, `show interfaces trunk`; add to the Cisco custom-fields-free description a short paragraph after "CDP/LLDP cabling":

```markdown
## VLAN sync (Cisco)

`show vlan brief` VLANs are created/updated in IPAM **per site** (marker `netbox-sync:`), interfaces get their VLAN linkage (`access` + untagged, `tagged`/`tagged-all` + native), and marked VLANs no longer reported by any switch at a site are deleted after each run. Manual VLANs are never modified or deleted.
```

- [ ] **Step 4: README (FA)** — mirror:

```markdown
## همگام‌سازی VLAN (سیسکو)

VLANهای `show vlan brief` به‌صورت **per-site** در IPAM ساخته/به‌روزرسانی می‌شوند (علامت `netbox-sync:`)، رابط‌ها اتصال VLAN خود را دریافت می‌کنند (`access` با untagged، `tagged`/`tagged-all` با native)، و VLANهای علامت‌داری که دیگر هیچ سوئیچی در آن سایت گزارش نکند پس از هر اجرا حذف می‌شوند. VLANهای دستی هرگز تغییر یا حذف نمی‌شوند.
```

- [ ] **Step 5: Verify**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m py_compile netbox_sync\sync.py netbox_sync\collectors\cisco.py
.\.venv\Scripts\python.exe -c "from netbox_sync.sync import run_sync; print('WIRING OK')"
```
Expected: 93+ pass, compile OK, WIRING OK.

- [ ] **Step 6: Commit + push**

```bash
git add netbox_sync/collectors/cisco.py netbox_sync/sync.py README.md
git commit -m "Wire VLAN sync into collector and run_sync; document (EN+FA)"
git push origin main
```
