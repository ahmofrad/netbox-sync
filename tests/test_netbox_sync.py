"""Tests for NetBox-facing sync logic, using in-memory fakes (no network).

Covers:
- sync_inventory reconciliation semantics (stale deletion, duplicate
  cleanup, update-vs-create) -- characterization tests guarding the refactor
- get_or_create_inventory_role name-based resolution + caching
- inventory role resolution at collector call sites (regression guard for
  the hardcoded ROLE_* ID migration)
- config validation, log-level filtering, TLS/SSH security options
"""
from types import SimpleNamespace

import pytest

import netbox_sync.collectors.brocade as brc
import netbox_sync.collectors.msa as msa
import netbox_sync.config as cfg
import netbox_sync.netbox as nbx
from netbox_sync import utils


# ── In-memory pynetbox fakes ─────────────────────────────────────────────────

class FakeRecord:
    def __init__(self, id, endpoint=None, **fields):
        self.id = id
        self._endpoint = endpoint
        self.deleted = False
        for k, v in fields.items():
            setattr(self, k, v)

    def delete(self):
        self.deleted = True
        if self._endpoint is not None:
            self._endpoint.deleted_ids.append(self.id)


class FakeEndpoint:
    """Mimics a pynetbox endpoint: filter/get/create/update."""

    def __init__(self, items=None):
        self.items = list(items or [])
        for i in self.items:
            i._endpoint = self
        self.created = []
        self.updated = []
        self.deleted_ids = []
        self.create_calls = 0   # invocation counts — proves bulk usage
        self.update_calls = 0
        self._next_id = 9000

    def _alive(self):
        return [i for i in self.items if not i.deleted]

    def filter(self, **kwargs):
        return [i for i in self._alive()
                if all(getattr(i, k, None) == v for k, v in kwargs.items())]

    def get(self, **kwargs):
        matches = self.filter(**kwargs)
        return matches[0] if matches else None

    def create(self, payload):
        self.create_calls += 1
        payloads = payload if isinstance(payload, list) else [payload]
        self.created.extend(payloads)
        records = []
        for p in payloads:
            rec = FakeRecord(self._next_id, endpoint=self, **p)
            self._next_id += 1
            # NetBox's device_id filter matches the device relation — model it
            if not hasattr(rec, "device_id") and hasattr(rec, "device"):
                rec.device_id = rec.device
            self.items.append(rec)
            records.append(rec)
        return records if isinstance(payload, list) else records[0]

    def update(self, payload_list):
        self.update_calls += 1
        self.updated.extend(payload_list)
        return True


@pytest.fixture(autouse=True)
def _clear_role_cache():
    for cache in (nbx._INVENTORY_ROLE_CACHE, nbx._MANUFACTURER_CACHE,
                  nbx._ROLE_CACHE, nbx._SITE_CACHE, nbx._DEVICE_TYPE_CACHE):
        cache.clear()
    yield
    for cache in (nbx._INVENTORY_ROLE_CACHE, nbx._MANUFACTURER_CACHE,
                  nbx._ROLE_CACHE, nbx._SITE_CACHE, nbx._DEVICE_TYPE_CACHE):
        cache.clear()


def _fake_api(**endpoints):
    return SimpleNamespace(dcim=SimpleNamespace(**endpoints))


# ── sync_inventory reconciliation ────────────────────────────────────────────

def _item(serial, name="Item"):
    return {"name": name, "manufacturer": "HPE", "part_number": "PN",
            "serial": serial, "description": "", "role": 4}


def test_sync_inventory_deletes_stale_and_dupes_and_upserts(monkeypatch):
    existing = [
        FakeRecord(1, serial="KEEP", device_id=7),
        FakeRecord(2, serial="STALE", device_id=7),
        FakeRecord(3, serial="DUP", device_id=7),
        FakeRecord(4, serial="DUP", device_id=7),
    ]
    ep = FakeEndpoint(existing)
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_items=ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)

    nbx.sync_inventory(7, {"KEEP": _item("KEEP"),
                           "DUP": _item("DUP"),
                           "NEW": _item("NEW")})

    # STALE removed; both DUP duplicates removed
    assert set(ep.deleted_ids) == {2, 3, 4}
    # KEEP updated in place, DUP + NEW created fresh
    assert {u["id"] for u in ep.updated} == {1}
    assert {c["serial"] for c in ep.created} == {"DUP", "NEW"}
    # Exactly one live item per serial at the end
    live_serials = sorted(i.serial for i in ep._alive())
    assert live_serials == ["DUP", "KEEP", "NEW"]


def test_sync_inventory_single_fetch_and_no_per_item_get(monkeypatch):
    """The refactor must not re-fetch the list or .get() per item (N+1)."""
    ep = FakeEndpoint([FakeRecord(1, serial="A", device_id=7)])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_items=ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)

    filter_calls = []
    orig_filter = ep.filter
    def counting_filter(**kw):
        filter_calls.append(kw)
        return orig_filter(**kw)
    ep.filter = counting_filter
    def fail_get(**kw):
        raise AssertionError("per-item .get() must not be used")
    ep.get = fail_get

    nbx.sync_inventory(7, {"A": _item("A"), "B": _item("B")})
    assert len(filter_calls) == 1


# ── inventory item role resolution ───────────────────────────────────────────

def _roles_endpoint():
    return FakeEndpoint([
        FakeRecord(42, name="HDD", slug="hdd"),
        FakeRecord(43, name="SSD", slug="ssd"),
        FakeRecord(44, name="PSU", slug="psu"),
        FakeRecord(45, name="Controller", slug="controller"),
        FakeRecord(46, name="SAS Exp", slug="sas-exp"),
        FakeRecord(47, name="SFP", slug="sfp"),
        FakeRecord(48, name="Fan", slug="fan"),
        FakeRecord(49, name="Module", slug="module"),
    ])


def test_inventory_role_resolved_by_name_and_cached(monkeypatch):
    ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    rid1 = nbx.get_or_create_inventory_role("HDD")
    rid2 = nbx.get_or_create_inventory_role("HDD")

    assert rid1 == rid2
    assert len(ep.created) == 1
    assert ep.created[0]["name"] == "HDD"
    assert ep.created[0]["slug"] == "hdd"


def test_inventory_role_finds_existing_by_name(monkeypatch):
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))
    assert nbx.get_or_create_inventory_role("SSD") == 43
    assert ep.created == []


def test_manufacturer_lookup_is_cached(monkeypatch):
    """Repeated get_or_create_manufacturer calls must not re-hit the API
    (inventory sync calls it once per item — N+1 without caching)."""
    ep = FakeEndpoint([FakeRecord(7, name="HPE", slug="hpe")])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(manufacturers=ep))
    calls = []
    orig_get = ep.get
    def counting_get(**kw):
        calls.append(kw)
        return orig_get(**kw)
    ep.get = counting_get

    assert nbx.get_or_create_manufacturer("HPE") == 7
    assert nbx.get_or_create_manufacturer("HPE") == 7
    assert len(calls) == 1


def test_device_role_lookup_is_cached(monkeypatch):
    ep = FakeEndpoint([FakeRecord(3, name="Server", slug="server")])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(device_roles=ep))
    calls = []
    orig_get = ep.get
    def counting_get(**kw):
        calls.append(kw)
        return orig_get(**kw)
    ep.get = counting_get

    assert nbx.get_or_create_role("Server") == 3
    assert nbx.get_or_create_role("Server") == 3
    assert len(calls) == 1


def test_storage_collectors_resolve_roles_by_name(monkeypatch):
    """Collector call sites must use name-resolved role IDs, not hardcoded
    constants (regression guard for the ROLE_* migration)."""
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    inv = {}
    add = utils._make_add_item(inv)
    msa._collect_disk_storage(
        {"serial-number": "D1", "drive-type": "SAS", "size": "1.8TB"}, add)
    msa._collect_disk_storage(
        {"serial-number": "D2", "drive-type": "SSD", "size": "480GB"}, add)
    msa._collect_psu_storage({"serial-number": "P1", "location": "1.1"}, add)
    msa._collect_controller_storage(
        {"serial-number": "C1", "controller-id": "A"}, add)
    msa._collect_fru_storage({"serial-number": "F1", "fru-name": "Exp"}, add)

    assert inv["D1"]["role"] == 42   # HDD by name
    assert inv["D2"]["role"] == 43   # SSD by name
    assert inv["P1"]["role"] == 44   # PSU by name
    assert inv["C1"]["role"] == 45   # Controller by name
    assert inv["F1"]["role"] == 46   # SAS Exp by name
    assert ep.created == []          # all resolved, none created


# ── Cisco inventory role classification ──────────────────────────────────────

def test_cisco_inventory_roles_classified(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ep = _roles_endpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_item_roles=ep))

    inv = {}
    add = utils._make_add_item(inv)
    cisco._inventory_item_from_row(
        {"name": "Power Supply Module 0", "descr": "350W AC Power Supply",
         "pid": "PWR-C1-350WAC", "vid": "V01", "sn": "LIT23456789"}, add)
    cisco._inventory_item_from_row(
        {"name": "Fan Tray 0", "descr": "Fan Tray",
         "pid": "C9300-FAN-1", "vid": "V01", "sn": "FAN123456"}, add)
    cisco._inventory_item_from_row(
        {"name": "GigabitEthernet1/1/1", "descr": "1000BaseSX SFP",
         "pid": "GLC-SX-MMD", "vid": "V01", "sn": "FNS12345678"}, add)
    cisco._inventory_item_from_row(
        {"name": "Switch 1", "descr": "C9300-48U",
         "pid": "C9300-48U", "vid": "V02", "sn": "FOC2345X0AB"}, add)

    assert inv["LIT23456789"]["role"] == 44   # PSU
    assert inv["FAN123456"]["role"] == 48     # Fan
    assert inv["FNS12345678"]["role"] == 47   # SFP
    assert inv["FOC2345X0AB"]["role"] == 49   # Module
    assert inv["LIT23456789"]["part_number"] == "PWR-C1-350WAC"
    assert ep.created == []


# ── Cisco device ensure ──────────────────────────────────────────────────────

def test_ensure_cisco_device_creates_with_custom_fields(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    dev_id = nbx.ensure_cisco_device({
        "ip": "192.0.2.65", "serial": "FOC2345X0AB", "model": "C9300-48U",
        "hostname": "SW1", "manufacturer": "Cisco", "firmware": "16.9.4",
    })
    assert len(devices_ep.created) == 1
    payload = devices_ep.created[0]
    assert payload["serial"] == "FOC2345X0AB"
    assert payload["status"] == "active"
    assert payload["custom_fields"]["cisco_ip"] == "192.0.2.65"
    assert payload["custom_fields"]["cisco_enabled"] is True
    assert payload["custom_fields"]["cisco_model"] == "C9300-48U"
    assert dev_id is not None


# ── Cisco interface sync ─────────────────────────────────────────────────────

def test_sync_cisco_interfaces_update_create_delete(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="Gi1/0/1", device_id=7),
        FakeRecord(2, name="Gi1/0/9", device_id=7),   # stale -> deleted
    ])
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))

    ports = [
        {"port": "Gi1/0/1", "name": "Uplink", "status": "connected",
         "vlan": "trunk", "duplex": "full", "speed": "1000",
         "type": "1000BaseSX SFP"},
        {"port": "Gi1/0/2", "name": "", "status": "notconnect",
         "vlan": "1", "duplex": "auto", "speed": "auto",
         "type": "10/100/1000BaseTX"},
    ]
    cisco.sync_cisco_interfaces(7, ports)

    assert {u["id"] for u in ifaces_ep.updated} == {1}
    assert ifaces_ep.updated[0]["type"] == "1000base-x-sfp"
    assert ifaces_ep.updated[0]["enabled"] is True
    assert len(ifaces_ep.created) == 1
    assert ifaces_ep.created[0]["name"] == "Gi1/0/2"
    assert ifaces_ep.created[0]["type"] == "other"
    assert ifaces_ep.created[0]["enabled"] is False
    assert ifaces_ep.deleted_ids == [2]
    # bulk: one HTTP call per operation, not one per interface
    assert ifaces_ep.update_calls == 1
    assert ifaces_ep.create_calls == 1


def test_interface_and_vlan_syncs_are_bulk(monkeypatch):
    """Performance guard: N items must sync in O(1) HTTP calls, not O(N)."""
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint([
        FakeRecord(i, name=f"Gi1/0/{i}", device_id=7) for i in range(1, 8)
    ])
    api = SimpleNamespace(
        dcim=SimpleNamespace(interfaces=ifaces_ep),
        ipam=SimpleNamespace(vlans=FakeEndpoint()))
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [{"port": f"Gi1/0/{i}", "name": "", "status": "connected",
              "vlan": "10", "duplex": "full", "speed": "1000",
              "type": "10/100/1000BaseTX"} for i in range(1, 8)]
    cisco.sync_cisco_interfaces(7, ports)
    assert ifaces_ep.update_calls == 1          # 7 interfaces, 1 bulk PATCH

    cisco.sync_interface_vlans(7, ports, [], {10: 110})
    assert ifaces_ep.update_calls == 2          # +1 more bulk PATCH for all VLANs


def test_inventory_sync_is_bulk(monkeypatch):
    ep = FakeEndpoint([FakeRecord(1, serial="A", device_id=7)])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: _fake_api(inventory_items=ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda name: 5)

    nbx.sync_inventory(7, {"A": _item("A"), "B": _item("B"), "C": _item("C")})
    assert ep.update_calls == 1                 # 1 update (A) in one call
    assert ep.create_calls == 1                 # 2 creates (B, C) in one call
    assert len(ep.created) == 2


# ── Cisco CDP cable sync ─────────────────────────────────────────────────────

def _cisco_cable_api(local_ifaces, peer_dev, peer_ifaces, cables):
    return _fake_api(
        devices=FakeEndpoint([peer_dev] if peer_dev else []),
        interfaces=FakeEndpoint(local_ifaces + peer_ifaces),
        cables=FakeEndpoint(cables),
    )

_PEER = FakeRecord(5, name="SW2")
_LOCAL_IFACE = FakeRecord(11, name="Gi1/0/1", device_id=7)
_PEER_IFACE = FakeRecord(55, name="Gi1/0/24", device_id=5)
_NEIGHBORS = [{"device_id": "SW2", "platform": "", "ip": None,
               "local_intf": "GigabitEthernet1/0/1",
               "remote_intf": "GigabitEthernet1/0/24"}]


def test_cdp_cable_created_when_both_ends_resolve(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert len(api.dcim.cables.created) == 1
    payload = api.dcim.cables.created[0]
    assert payload["a_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 11}]
    assert payload["b_terminations"] == [
        {"object_type": "dcim.interface", "object_id": 55}]
    assert payload["description"].startswith(cisco.CABLE_MARKER)


def test_cdp_cable_dedupes_existing_marked(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(9, device_id=7, description="netbox-sync: cdp old",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [marked])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.created == []          # no duplicate
    assert {u["id"] for u in api.dcim.cables.updated} == {9}
    assert api.dcim.cables.deleted_ids == []      # seen -> kept


def test_cdp_cable_skips_unresolvable_neighbor(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], None, [], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, [{"device_id": "UNKNOWN", "platform": "",
                               "ip": None, "local_intf": "GigabitEthernet1/0/1",
                               "remote_intf": "Gi0/1"}])
    assert api.dcim.cables.created == []


def test_cdp_cable_preserves_unmarked_and_conflicts(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    manual = FakeRecord(8, device_id=7, description="manual doc",
                        a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                        b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS)

    assert api.dcim.cables.created == []        # conflict -> no create
    assert api.dcim.cables.deleted_ids == []    # manual cable preserved


def test_cdp_cable_deletes_stale_marked(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    stale = FakeRecord(9, device_id=7, description="netbox-sync: cdp old",
                       a_terminations=[{"object_type": "dcim.interface", "object_id": 11}],
                       b_terminations=[{"object_type": "dcim.interface", "object_id": 55}])
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [stale])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, [])   # nothing seen this run

    assert api.dcim.cables.deleted_ids == [9]


class _GLO:
    """Stand-in for pynetbox GenericListObject (attribute access only)."""
    def __init__(self, object_id, object_type="dcim.interface"):
        self.object_id = object_id
        self.object_type = object_type


def test_cable_iface_ids_handles_generic_objects():
    """pynetbox returns GenericListObject terminations, not dicts — the
    dedupe must parse them (this was the cable-flap root cause)."""
    import netbox_sync.collectors.cisco as cisco
    cable = SimpleNamespace(
        a_terminations=[{"object_id": 1}, _GLO(2)],
        b_terminations=[_GLO(3), {"object_id": 4, "object_type": "x"}])
    assert sorted(cisco._cable_iface_ids(cable)) == [1, 2, 3, 4]


# ── FortiGate token file ─────────────────────────────────────────────────────

def test_fortigate_token_file_parsing(tmp_path):
    f = tmp_path / "tokens.txt"
    f.write_text(
        "# comment line\n"
        "\n"
        "172.31.1.1 token-one\n"
        "172.31.1.2:8443 token-two\n"
        "badline\n",
        encoding="utf-8")
    tokens = cfg._load_fortigate_tokens(str(f))
    assert tokens == {"172.31.1.1": (443, "token-one"),
                      "172.31.1.2": (8443, "token-two")}


def test_fortigate_token_file_missing(tmp_path):
    assert cfg._load_fortigate_tokens(str(tmp_path / "nope.txt")) == {}


def test_validate_config_fortigate_requirements(monkeypatch, tmp_path):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("FORTIGATE_USER", raising=False)
    monkeypatch.delenv("FORTIGATE_PASS", raising=False)
    monkeypatch.setenv("FORTIGATE_RANGES", "192.0.2.0/29")
    f = tmp_path / "tokens.txt"
    f.write_text("192.0.2.1 tok\n", encoding="utf-8")
    monkeypatch.setenv("FORTIGATE_TOKEN_FILE", str(f))
    with pytest.raises(RuntimeError, match="FORTIGATE_USER"):
        cfg._validate_config()
    monkeypatch.setenv("FORTIGATE_USER", "u")
    monkeypatch.setenv("FORTIGATE_PASS", "p")
    cfg._validate_config()   # creds + non-empty token file -> passes


# ── FortiGate device + interfaces ────────────────────────────────────────────

def test_ensure_fortigate_device_creates(monkeypatch):
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(devices=devices_ep))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)
    monkeypatch.setattr(nbx, "find_device", lambda *a, **k: None)

    nbx.ensure_fortigate_device({
        "ip": "192.0.2.70", "serial": "FGT60FTK21000001",
        "model": "FortiGate 60F", "hostname": "FGT-DC-01",
        "manufacturer": "Fortinet", "firmware": "v7.2.4"})
    payload = devices_ep.created[0]
    assert payload["serial"] == "FGT60FTK21000001"
    assert payload["custom_fields"]["fortigate_ip"] == "192.0.2.70"
    assert payload["custom_fields"]["fortigate_enabled"] is True


def test_sync_fortigate_interfaces_bulk_and_vlan_subif(monkeypatch):
    import netbox_sync.collectors.fortigate as fg
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="port1", device_id=7),
        FakeRecord(2, name="port9", device_id=7, mgmt_only=False),
    ])
    api = _fake_api(interfaces=ifaces_ep)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"name": "port1", "link": True, "speed_mbps": 1000,
         "type": "physical", "ip": "", "vlanid": None, "parent": "",
         "alias": "UPLINK-CORE"},
        {"name": "port1.10", "link": True, "speed_mbps": 1000,
         "type": "vlan", "ip": "10.10.10.1/24", "vlanid": 10, "parent": "port1",
         "alias": ""},
    ]
    fg.sync_fortigate_interfaces(7, ports, {10: 110})

    by_name = {}
    for u in ifaces_ep.updated:
        rec = next(i for i in ifaces_ep.items if i.id == u["id"])
        by_name[rec.name] = u
    assert by_name["port1"]["type"] == "1000base-t"
    assert by_name["port1"]["label"] == "UPLINK-CORE"
    created = {c["name"]: c for c in ifaces_ep.created}
    assert created["port1.10"]["type"] == "virtual"
    assert created["port1.10"]["untagged_vlan"] == 110
    assert created["port1.10"]["mode"] == "tagged"
    assert created["port1.10"]["parent"] == 1     # subinterface under its parent
    assert "label" not in created["port1.10"]   # empty alias -> no label key
    assert ifaces_ep.deleted_ids == [2]
    assert ifaces_ep.update_calls == 1 and ifaces_ep.create_calls == 1


def test_sync_fortigate_interfaces_lag_and_member_linkage(monkeypatch):
    import netbox_sync.collectors.fortigate as fg
    ifaces_ep = FakeEndpoint([
        FakeRecord(1, name="port33", device_id=7),
        FakeRecord(2, name="port34", device_id=7),
    ])
    api = _fake_api(interfaces=ifaces_ep)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ports = [
        {"name": "port33", "link": True, "speed_mbps": 10000,
         "type": "physical", "ip": "", "vlanid": None, "parent": "", "alias": ""},
        {"name": "port34", "link": True, "speed_mbps": 10000,
         "type": "physical", "ip": "", "vlanid": None, "parent": "", "alias": ""},
        {"name": "Core Switch", "link": True, "speed_mbps": None,
         "type": "lag", "members": ["port33", "port34"], "ip": "",
         "vlanid": None, "parent": "", "alias": ""},
    ]
    fg.sync_fortigate_interfaces(7, ports, {})

    created = {c["name"]: c for c in ifaces_ep.created}
    assert created["Core Switch"]["type"] == "lag"
    lag_id = next(i.id for i in ifaces_ep.items if i.name == "Core Switch")
    by_id = {u["id"]: u for u in ifaces_ep.updated}
    assert by_id[1]["lag"] == lag_id
    assert by_id[2]["lag"] == lag_id


def test_cdp_cables_protocol_label(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    api = _cisco_cable_api([_LOCAL_IFACE], _PEER, [_PEER_IFACE], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sync_cdp_cables(7, _NEIGHBORS, protocol="lldp")
    assert " lldp " in api.dcim.cables.created[0]["description"]


def test_ensure_svi_interface_creates_virtual_with_vlan(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))

    iid = cisco.ensure_svi_interface(7, "Vlan50", {50: 500})
    created = ifaces_ep.created[0]
    assert created["name"] == "Vlan50"
    assert created["type"] == "virtual"
    assert created["untagged_vlan"] == 500
    assert created["mgmt_only"] is True
    assert iid is not None

    # second call reuses
    cisco.ensure_svi_interface(7, "Vlan50", {50: 500})
    assert ifaces_ep.create_calls == 1


def test_ensure_svi_interface_non_vlan_name(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    ifaces_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox", lambda: _fake_api(interfaces=ifaces_ep))
    cisco.ensure_svi_interface(7, "Loopback0", {})
    assert "untagged_vlan" not in ifaces_ep.created[0]


# ── IPAM prefix sync ─────────────────────────────────────────────────────────

def test_prefix_from_ip_and_iface_addr():
    import netbox_sync.ipam as ipam
    assert ipam._prefix_from_ip("172.31.2.1 255.255.255.0") == "172.31.2.0/24"
    assert ipam._prefix_from_ip("10.19.128.1 255.255.255.0") == "10.19.128.0/24"
    assert ipam._prefix_from_ip("79.127.120.184 255.255.255.240") == "79.127.120.176/28"
    assert ipam._prefix_from_ip("0.0.0.0 0.0.0.0") is None
    assert ipam._prefix_from_ip("") is None
    assert ipam._prefix_from_ip(None) is None
    assert ipam._iface_addr_with_prefixlen("172.31.2.1 255.255.255.0") == \
        ("172.31.2.1/24", "172.31.2.1")
    assert ipam._iface_addr_with_prefixlen("0.0.0.0 0.0.0.0") == (None, None)
    assert ipam._iface_addr_with_prefixlen("") == (None, None)
    assert ipam._prefix_from_ip("10.19.128.1 255.255.255.0") == "10.19.128.0/24"
    assert ipam._prefix_from_ip("79.127.120.184 255.255.255.240") == "79.127.120.176/28"
    assert ipam._prefix_from_ip("0.0.0.0 0.0.0.0") is None
    assert ipam._prefix_from_ip("") is None
    assert ipam._prefix_from_ip(None) is None


def _prefix_api(prefix_items):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(prefixes=FakeEndpoint(prefix_items),
                             ip_addresses=FakeEndpoint()))


def test_ensure_prefix_create_refresh_and_manual(monkeypatch):
    import netbox_sync.ipam as ipam
    marked = FakeRecord(50, prefix="10.0.0.0/24",
                        description="netbox-sync: last seen OLD")
    manual = FakeRecord(51, prefix="10.1.0.0/24", description="manual prefix")
    api = _prefix_api([marked, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # marked existing -> refreshed with site+vlan
    pid = ipam.ensure_prefix("10.0.0.0/24", 3, 110, "FGT-DC-01", "VLAN10")
    assert pid == 50
    assert {u["id"] for u in api.ipam.prefixes.updated} == {50}
    assert api.ipam.prefixes.updated[0]["vlan"] == 110
    assert api.ipam.prefixes.updated[0]["site"] == 3

    # manual existing -> reused untouched
    pid = ipam.ensure_prefix("10.1.0.0/24", 3, 111, "FGT-DC-01", "VLAN11")
    assert pid == 51
    assert len(api.ipam.prefixes.updated) == 1   # no update for manual

    # missing -> created marked with site+vlan
    pid = ipam.ensure_prefix("10.2.0.0/24", 3, 112, "FGT-DC-01", "VLAN12")
    assert api.ipam.prefixes.created[0]["prefix"] == "10.2.0.0/24"
    assert api.ipam.prefixes.created[0]["site"] == 3
    assert api.ipam.prefixes.created[0]["vlan"] == 112
    assert api.ipam.prefixes.created[0]["description"].startswith("netbox-sync:")


def _host_ip_api(ip_items, iface_items, prefix_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint(iface_items)),
        ipam=SimpleNamespace(ip_addresses=FakeEndpoint(ip_items),
                             prefixes=FakeEndpoint(prefix_items or [])))


def test_ensure_host_ip_create_with_mask_and_assignment(monkeypatch):
    import netbox_sync.ipam as ipam
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    api = _host_ip_api([], [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = ipam.ensure_host_ip(7, "172.31.2.1/24", "MGMT54",
                                "FGT-DC-01", "AP MGMT")

    created = api.ipam.ip_addresses.created[0]
    assert created["address"] == "172.31.2.1/24"
    assert created["status"] == "active"
    assert created["description"].startswith("netbox-sync: if ")
    assert created["assigned_object_type"] == "dcim.interface"
    assert created["assigned_object_id"] == 70
    assert api.ipam.ip_addresses.updated == []   # created with assignment
    assert ip_id is not None


def test_ensure_host_ip_reuses_existing(monkeypatch):
    import netbox_sync.ipam as ipam
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    existing = FakeRecord(50, address="172.31.2.1",
                        assigned_object_type=None, assigned_object_id=None)
    api = _host_ip_api([existing], [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = ipam.ensure_host_ip(7, "172.31.2.1/24", "MGMT54",
                                "FGT-DC-01", "AP MGMT")
    assert ip_id == 50
    assert api.ipam.ip_addresses.created == []
    assert api.ipam.ip_addresses.updated[0]["assigned_object_id"] == 70


def test_containing_prefix_longest_match(monkeypatch):
    import netbox_sync.ipam as ipam
    broad = FakeRecord(50, prefix="172.31.0.0/16")
    specific = FakeRecord(51, prefix="172.31.2.0/24")
    api = _host_ip_api([], [], [broad, specific])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    p = ipam._containing_prefix("172.31.2.44")
    assert p.id == 51

    assert ipam._containing_prefix("10.9.9.9") is None


def test_sweep_stale_prefixes(monkeypatch):
    import netbox_sync.ipam as ipam
    seen = FakeRecord(50, prefix="10.0.0.0/24", site_id=3,
                      description="netbox-sync: last seen SW1")
    stale = FakeRecord(51, prefix="10.1.0.0/24", site_id=3,
                       description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, prefix="10.2.0.0/24", site_id=3,
                        description="manual prefix")
    api = _prefix_api([seen, stale, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ipam.sweep_stale_prefixes(3, {"10.0.0.0/24", "10.9.0.0/24"})

    assert api.ipam.prefixes.deleted_ids == [51]


def test_sweep_stale_host_ips(monkeypatch):
    import netbox_sync.ipam as ipam
    seen = FakeRecord(50, address="172.31.2.1/24", device_id=7,
                      description="netbox-sync: if FGT AP MGMT")
    stale = FakeRecord(51, address="172.31.9.9/24", device_id=7,
                       description="netbox-sync: if FGT OLD")
    mgmt = FakeRecord(52, address="172.31.5.1/32", device_id=7,
                      description="netbox-sync: mgmt")
    manual = FakeRecord(53, address="10.1.1.1/24", device_id=7,
                        description="manual")
    api = _host_ip_api([seen, stale, mgmt, manual], [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ipam.sweep_stale_host_ips(7, {"172.31.2.1"})

    assert api.ipam.ip_addresses.deleted_ids == [51]


def test_site_vlan_index(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    g = FakeRecord(8, name="BD1", description="netbox-sync: vtp=snapp",
                    scope_type="dcim.site", scope_id=3)
    manual_g = FakeRecord(9, name="X", description="manual",
                          scope_type="dcim.site", scope_id=3)
    vlans = [FakeRecord(50, vid=10, group_id=8),
             FakeRecord(51, vid=20, group_id=8),
             FakeRecord(52, vid=10, group_id=9)]
    api = _vlan_api(vlans, [g, manual_g])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    index = cisco._site_vlan_index(3)
    assert index == {10: [(8, 50)], 20: [(8, 51)]}


def test_resolve_fortigate_vlans_paths():
    import netbox_sync.collectors.fortigate as fg
    site_index = {10: [(8, 50)], 20: [(8, 51), (9, 60)], 30: []}
    vlans = [{"vid": 10, "name": "A", "status": "active"},
             {"vid": 20, "name": "B", "status": "active"},
             {"vid": 30, "name": "C", "status": "active"},
             {"vid": 40, "name": "D", "status": "active"}]
    get_mac = lambda vid: "00:09:0f:09:00:26" if vid == 20 else None
    lookup = lambda vid, mac: 9 if (vid, mac) == (20, "00:09:0f:09:00:26") else None

    vid_map, missing = fg.resolve_fortigate_vlans(site_index, vlans, get_mac, lookup)

    assert vid_map == {10: 50, 20: 60}     # unique reused; overlap resolved
    assert [v["vid"] for v in missing] == [30, 40]   # none + unresolved overlap


# ── config validation ────────────────────────────────────────────────────────

REQUIRED_VARS = ["NETBOX_URL", "NETBOX_TOKEN", "REDFISH_USER", "REDFISH_PASS",
                 "STORAGE_USER", "STORAGE_PASS", "SWITCH_USER", "SWITCH_PASS"]


def test_validate_config_ok_when_all_vars_present(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    cfg._validate_config()  # must not raise


def test_validate_config_lists_missing_vars(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("NETBOX_TOKEN")
    monkeypatch.delenv("SWITCH_PASS")
    with pytest.raises(RuntimeError, match="NETBOX_TOKEN"):
        cfg._validate_config()
    with pytest.raises(RuntimeError, match="SWITCH_PASS"):
        cfg._validate_config()


# ── Cisco config ─────────────────────────────────────────────────────────────

def test_cisco_ranges_default_empty_and_parse(monkeypatch):
    import importlib
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    importlib.reload(cfg)
    assert cfg.CISCO_RANGES == []
    monkeypatch.setenv("CISCO_RANGES", "192.0.2.0/29, 198.51.100.0/29")
    importlib.reload(cfg)
    assert cfg.CISCO_RANGES == ["192.0.2.0/29", "198.51.100.0/29"]
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    importlib.reload(cfg)


def test_empty_range_env_disables_family(monkeypatch):
    """Set-but-empty range env vars must disable the family ([]), NOT fall
    back to the placeholder defaults — this is how users turn families off."""
    import importlib
    monkeypatch.setenv("BMC_RANGES", "")
    monkeypatch.setenv("STORAGE_RANGES", "")
    monkeypatch.setenv("SAN_RANGES", "")
    importlib.reload(cfg)
    assert cfg.BMC_RANGES == []
    assert cfg.STORAGE_RANGES == []
    assert cfg.SAN_RANGES == []
    # Unset env vars still fall back to the documented placeholder defaults
    monkeypatch.delenv("BMC_RANGES")
    importlib.reload(cfg)
    assert cfg.BMC_RANGES == cfg.DEFAULT_BMC_RANGES


def test_site_ip_map_parsing_and_sort(monkeypatch):
    import importlib
    monkeypatch.setenv(
        "SITE_IP_MAP",
        "172.31.0.0/16:HQ,172.31.1.0/24:Branch,bad-entry,10.0.0.0/8:Net")
    importlib.reload(cfg)
    assert [(str(n), s) for n, s in cfg.SITE_IP_MAP] == [
        ("172.31.1.0/24", "Branch"),   # /24 beats /16 beats /8 (longest first)
        ("172.31.0.0/16", "HQ"),
        ("10.0.0.0/8", "Net"),
    ]
    monkeypatch.setenv("SITE_IP_MAP", "not-a-cidr:X")
    importlib.reload(cfg)
    assert cfg.SITE_IP_MAP == []       # invalid CIDR skipped, no crash
    monkeypatch.delenv("SITE_IP_MAP", raising=False)
    importlib.reload(cfg)
    assert cfg.SITE_IP_MAP == []       # unset -> empty (backward compatible)


def test_validate_config_requires_cisco_creds_only_when_ranges_set(monkeypatch):
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("CISCO_RANGES", raising=False)
    monkeypatch.delenv("CISCO_USER", raising=False)
    monkeypatch.delenv("CISCO_PASS", raising=False)
    cfg._validate_config()  # no ranges -> no creds needed

    monkeypatch.setenv("CISCO_RANGES", "192.0.2.0/29")
    with pytest.raises(RuntimeError, match="CISCO_USER"):
        cfg._validate_config()


# ── log level filtering ──────────────────────────────────────────────────────

def test_debug_logs_hidden_by_default(capsys, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_LEVEL", "INFO")
    cfg.log("DEBUG", "dbg-hidden")
    cfg.log("INFO", "info-shown")
    out = capsys.readouterr().out
    assert "dbg-hidden" not in out
    assert "info-shown" in out


def test_debug_logs_shown_when_level_is_debug(capsys, monkeypatch):
    monkeypatch.setattr(cfg, "LOG_LEVEL", "DEBUG")
    cfg.log("DEBUG", "dbg-shown")
    assert "dbg-shown" in capsys.readouterr().out


# ── security options ─────────────────────────────────────────────────────────

class _FakeNetboxAPI:
    def __init__(self):
        self.http_session = SimpleNamespace(verify=None)


def test_netbox_tls_verify_defaults_off(monkeypatch):
    fake = _FakeNetboxAPI()
    monkeypatch.setattr(nbx.pynetbox, "api", lambda *a, **k: fake)
    monkeypatch.delenv("NETBOX_VERIFY_TLS", raising=False)
    monkeypatch.setattr(nbx, "nb", None)
    nbx.get_netbox()
    assert fake.http_session.verify is False


def test_netbox_tls_verify_can_be_enabled(monkeypatch):
    fake = _FakeNetboxAPI()
    monkeypatch.setattr(nbx.pynetbox, "api", lambda *a, **k: fake)
    monkeypatch.setenv("NETBOX_VERIFY_TLS", "true")
    monkeypatch.setattr(nbx, "nb", None)
    nbx.get_netbox()
    assert fake.http_session.verify is True


class _FakeTransport:
    def is_active(self):
        return True


class _FakeSSHClient:
    instance = None

    def __init__(self):
        _FakeSSHClient.instance = self
        self.policy = None
        self.host_keys_loaded = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_system_host_keys(self):
        self.host_keys_loaded = True

    def connect(self, **kwargs):
        pass

    def get_transport(self):
        return _FakeTransport()

    def close(self):
        pass


def test_ssh_host_key_policy_defaults_to_auto_add(monkeypatch):
    monkeypatch.setattr(brc.paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.delenv("SWITCH_STRICT_HOST_KEY", raising=False)
    brc.BrocadeSwitchSession("192.0.2.1").login()
    assert isinstance(_FakeSSHClient.instance.policy,
                      brc.paramiko.AutoAddPolicy)
    assert _FakeSSHClient.instance.host_keys_loaded is False


def test_ssh_host_key_policy_strict_when_enabled(monkeypatch):
    monkeypatch.setattr(brc.paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setenv("SWITCH_STRICT_HOST_KEY", "true")
    brc.BrocadeSwitchSession("192.0.2.1").login()
    assert isinstance(_FakeSSHClient.instance.policy,
                      brc.paramiko.RejectPolicy)
    assert _FakeSSHClient.instance.host_keys_loaded is True


# ── primary IPv4 ─────────────────────────────────────────────────────────────

def test_mgmt_prefixlen_from_ranges(monkeypatch):
    monkeypatch.setattr(utils, "BMC_RANGES", ["10.0.0.0/24"])
    monkeypatch.setattr(utils, "STORAGE_RANGES", [])
    monkeypatch.setattr(utils, "SAN_RANGES", [])
    monkeypatch.setattr(utils, "CISCO_RANGES", ["172.31.1.0/27"])
    assert utils._mgmt_prefixlen("10.0.0.5") == 24
    assert utils._mgmt_prefixlen("172.31.1.5") == 27
    assert utils._mgmt_prefixlen("192.0.2.9") == 32   # no range contains it
    assert utils._mgmt_prefixlen("junk") == 32        # invalid tolerated


def _ipam_api(ip_items, device_record, iface_items=None):
    # api.ipam is a separate pynetbox app from api.dcim — model both
    return SimpleNamespace(
        dcim=SimpleNamespace(
            devices=FakeEndpoint([device_record] if device_record else []),
            interfaces=FakeEndpoint(iface_items or [])),
        ipam=SimpleNamespace(ip_addresses=FakeEndpoint(ip_items)))


def test_primary_ip_created_assigned_and_set(monkeypatch):
    """Full flow: IPAM record created with range mask, a mgmt_only interface
    is created, the IP is ASSIGNED to it (NetBox requires assignment before
    primary_ip4 is accepted), then primary_ip4 is set."""
    monkeypatch.setattr(utils, "BMC_RANGES", [])
    monkeypatch.setattr(utils, "STORAGE_RANGES", [])
    monkeypatch.setattr(utils, "SAN_RANGES", [])
    monkeypatch.setattr(utils, "CISCO_RANGES", ["172.31.1.0/24"])
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    api = _ipam_api([], dev)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = nbx.ensure_primary_ip(7, "172.31.1.103", "F10-SW-W-02")

    created = api.ipam.ip_addresses.created[0]
    assert created["address"] == "172.31.1.103/24"
    assert created["dns_name"] == "f10-sw-w-02"
    assert created["description"] == "netbox-sync: mgmt"

    # mgmt interface created on the device
    assert len(api.dcim.interfaces.created) == 1
    iface = api.dcim.interfaces.created[0]
    assert iface["device"] == 7
    assert iface["type"] == "virtual"
    assert iface["mgmt_only"] is True

    # IP assigned to that interface, then device primary set
    iface_id = api.dcim.interfaces.items[-1].id
    assert {"id": ip_id, "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface_id} in api.ipam.ip_addresses.updated
    assert {u["id"] for u in api.dcim.devices.updated} == {7}
    assert api.dcim.devices.updated[0]["primary_ip4"] == ip_id


def test_primary_ip_reuses_existing_and_mgmt_iface(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    existing_ip = FakeRecord(50, address="172.31.1.103",
                             assigned_object_type=None, assigned_object_id=None)
    mgmt_iface = FakeRecord(60, name="mgmt", device_id=7, mgmt_only=True)
    api = _ipam_api([existing_ip], dev, [mgmt_iface])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    ip_id = nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    assert ip_id == 50
    assert api.ipam.ip_addresses.created == []      # reused IP record
    assert api.dcim.interfaces.created == []        # reused mgmt interface
    assert api.ipam.ip_addresses.updated[0]["assigned_object_id"] == 60


def test_primary_ip_skipped_when_assigned_to_other_device(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    foreign_iface = FakeRecord(99, name="Gi0/1", device_id=42)
    taken_ip = FakeRecord(50, address="172.31.1.103",
                          assigned_object_type="dcim.interface",
                          assigned_object_id=99)
    api = _ipam_api([taken_ip], dev, [])
    api.dcim.interfaces.items.append(foreign_iface)
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    # never hijack an IP assigned to another device
    assert api.dcim.devices.updated == []
    assert api.ipam.ip_addresses.updated == []


def test_primary_ip_no_write_when_already_correct(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=FakeRecord(50))
    own_iface = FakeRecord(60, name="mgmt", device_id=7, mgmt_only=True)
    own_ip = FakeRecord(50, address="172.31.1.103",
                        assigned_object_type="dcim.interface",
                        assigned_object_id=60)
    api = _ipam_api([own_ip], dev, [own_iface])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "172.31.1.103", "SW1")

    assert api.dcim.devices.updated == []
    assert api.ipam.ip_addresses.updated == []


def test_primary_ip_assigned_to_named_iface(monkeypatch):
    dev = FakeRecord(7, name="FGT-DC-01", primary_ip4=None)
    svi = FakeRecord(70, name="MGMT54", device_id=7)
    api = _ipam_api([], dev, [svi])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "192.0.2.70", "FGT-DC-01", iface_name="MGMT54")

    # no synthetic mgmt interface created; IP assigned to the named one
    assert api.dcim.interfaces.created == []
    upd = api.ipam.ip_addresses.updated[0]
    assert upd["assigned_object_id"] == 70
    assert api.dcim.devices.updated[0]["primary_ip4"] is not None


def test_primary_ip_named_iface_missing_falls_back_to_mgmt(monkeypatch):
    dev = FakeRecord(7, name="SW1", primary_ip4=None)
    api = _ipam_api([], dev, [])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    nbx.ensure_primary_ip(7, "192.0.2.70", "SW1", iface_name="Vlan999")

    # synthetic mgmt interface created as fallback
    assert api.dcim.interfaces.created[0]["name"] == "mgmt"


# ── Cisco VLAN sync ──────────────────────────────────────────────────────────

def _vlan_api(vlan_items, group_items=None):
    return SimpleNamespace(
        dcim=SimpleNamespace(interfaces=FakeEndpoint()),
        ipam=SimpleNamespace(vlans=FakeEndpoint(vlan_items),
                             vlan_groups=FakeEndpoint(group_items or [])))


def test_sync_cisco_vlans_create_update_and_manual_reuse(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    marked = FakeRecord(50, vid=10, group_id=8, description="netbox-sync: last seen OLD")
    manual = FakeRecord(51, vid=20, group_id=8, description="manual vlan")
    api = _vlan_api([marked, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    vid_map = cisco.sync_cisco_vlans(8, "SW1", [
        {"vid": 10, "name": "USERS", "status": "active"},
        {"vid": 20, "name": "SERVERS", "status": "active"},
        {"vid": 30, "name": "GUEST", "status": "active"},
    ])

    assert vid_map[10] == 50
    assert vid_map[20] == 51
    # marked existing -> updated; manual -> untouched; new -> created with group
    assert {u["id"] for u in api.ipam.vlans.updated} == {50}
    assert len(api.ipam.vlans.created) == 1
    assert api.ipam.vlans.created[0]["vid"] == 30
    assert api.ipam.vlans.created[0]["group"] == 8
    assert api.ipam.vlans.created[0]["description"].startswith(cisco.VLAN_MARKER)
    assert vid_map[30] is not None


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
    seen = FakeRecord(50, vid=10, group_id=8, description="netbox-sync: last seen SW1")
    stale = FakeRecord(51, vid=20, group_id=8, description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, vid=30, group_id=8, description="manual vlan")
    api = _vlan_api([seen, stale, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sweep_stale_vlans(8, {10, 40})

    assert api.ipam.vlans.deleted_ids == [51]


def test_ensure_vlan_group_reuses_by_key_and_names_next_bd(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    g1 = FakeRecord(60, name="BD1", description="netbox-sync: vtp=snapp",
                    scope_type="dcim.site", scope_id=3)
    g2 = FakeRecord(61, name="BD3", description="netbox-sync: vtp=other",
                    scope_type="dcim.site", scope_id=3)
    manual = FakeRecord(62, name="BD2", description="manual group",
                        scope_type="dcim.site", scope_id=3)
    api = _vlan_api([], [g1, g2, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # existing key -> reused, nothing created
    assert cisco.ensure_vlan_group(3, "snapp") == 60
    assert api.ipam.vlan_groups.created == []

    # new key -> created with next FREE BD number among marked groups (BD3+1)
    gid = cisco.ensure_vlan_group(3, "campus-b")
    created = api.ipam.vlan_groups.created[0]
    assert created["name"] == "BD4"
    assert created["slug"] == "bd4"
    assert created["description"] == "netbox-sync: vtp=campus-b"
    assert created["scope_type"] == "dcim.site"
    assert created["scope_id"] == 3
    assert gid is not None


def test_sweep_legacy_site_vlans(monkeypatch):
    import netbox_sync.collectors.cisco as cisco
    legacy = FakeRecord(50, vid=10, site_id=3, group=None,
                        description="netbox-sync: last seen SW1")
    grouped = FakeRecord(51, vid=10, site_id=None, group=8,
                         description="netbox-sync: last seen SW1")
    manual = FakeRecord(52, vid=20, site_id=3, group=None,
                        description="manual vlan")
    api = _vlan_api([legacy, grouped, manual])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    cisco.sweep_legacy_site_vlans(3)

    assert api.ipam.vlans.deleted_ids == [50]   # only the group-less marked one


def test_sweep_stale_groups_migration(monkeypatch):
    """Stale marked groups (case-variant duplicate, abandoned hostname
    fallback) are emptied and deleted; fed groups and manual groups stay."""
    import netbox_sync.collectors.cisco as cisco
    g_snapp   = FakeRecord(1, name="BD1", description="netbox-sync: vtp=snapp",
                           scope_type="dcim.site", scope_id=3)
    g_SNAPP   = FakeRecord(2, name="BD2", description="netbox-sync: vtp=Snapp",
                           scope_type="dcim.site", scope_id=3)
    g_fb      = FakeRecord(4, name="BD4", description="netbox-sync: vtp=f12-cctv-sw-02",
                           scope_type="dcim.site", scope_id=3)
    manual_g  = FakeRecord(9, name="X", description="manual group",
                           scope_type="dcim.site", scope_id=3)
    vlans = [
        FakeRecord(50, vid=201, group_id=1, description="netbox-sync: x"),
        FakeRecord(51, vid=202, group_id=2, description="netbox-sync: x"),
        FakeRecord(52, vid=20,  group_id=4, description="netbox-sync: x"),
        FakeRecord(53, vid=999, group_id=9, description="manual vlan"),
    ]
    api = _vlan_api(vlans, [g_snapp, g_SNAPP, g_fb, manual_g])
    monkeypatch.setattr(nbx, "get_netbox", lambda: api)

    # BD1(snapp) is fed this run; f12-cctv-sw-02 moved to a component group
    cisco._sweep_stale_groups(
        3, fed_group_ids={1},
        key_by_name={"f12-cctv-sw-02": "f_-1-cctv-sw"})

    # BD2 (case-variant) and BD4 (abandoned fallback) lost their VLANs,
    # then were deleted themselves; BD1 and the manual group untouched
    assert set(api.ipam.vlans.deleted_ids) == {51, 52}
    assert set(api.ipam.vlan_groups.deleted_ids) == {2, 4}


def test_interface_syncs_preserve_mgmt_interfaces(monkeypatch):
    """The synthetic mgmt interface must survive the stale-interface cleanup
    in both Cisco and SAN interface syncs."""
    import netbox_sync.collectors.brocade as brocade_mod
    import netbox_sync.collectors.cisco as cisco_mod
    for mod, sync_fn, port in (
            (cisco_mod, cisco_mod.sync_cisco_interfaces,
             {"port": "Gi1/0/1", "name": "", "status": "connected",
              "vlan": "1", "duplex": "full", "speed": "1000", "type": "10/100/1000BaseTX"}),
            (brocade_mod, brocade_mod.sync_san_interfaces,
             {"index": 0, "port": 0, "address": "010000", "media": "id",
              "speed": "N16", "state": "Online", "proto": "FC", "comment": ""})):
        ifaces_ep = FakeEndpoint([
            FakeRecord(1, name="mgmt", device_id=7, mgmt_only=True),
            FakeRecord(2, name="stale-iface", device_id=7, mgmt_only=False),
        ])
        monkeypatch.setattr(nbx, "get_netbox",
                            lambda: _fake_api(interfaces=ifaces_ep))
        if sync_fn is cisco_mod.sync_cisco_interfaces:
            sync_fn(7, [port])
        else:
            sync_fn(7, [port], [])
        assert 1 not in ifaces_ep.deleted_ids   # mgmt preserved
        assert 2 in ifaces_ep.deleted_ids       # stale removed
