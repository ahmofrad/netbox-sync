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
        self.created.append(payload)
        rec = FakeRecord(self._next_id, endpoint=self, **payload)
        self._next_id += 1
        self.items.append(rec)
        return rec

    def update(self, payload_list):
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
