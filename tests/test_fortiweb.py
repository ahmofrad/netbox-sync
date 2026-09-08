"""Tests for the FortiWeb WAF collector (parsers + auth header) and the
NetBox ensure/offline logic — against in-memory fakes, no hardware."""
import base64
import json
from types import SimpleNamespace

import pytest

import netbox_sync.collectors.fortiweb as fw
import netbox_sync.netbox as nbx


# ── live-captured fixtures (FortiWeb 1000E/1000F 7.2.12, 2026-09-08) ────────

STATUS_RESULTS = {
    "hostName": "FortiWeb-Azadegan",
    "serialNumber": "FV-1KET122900020",
    "operationMode": "Reverse Proxy",
    "haStatus": "Standalone",
    "firmwareVersion": "FortiWeb-1000E 7.2.12,build0437(GA),251023",
    "up_days": "285",
}

STATUS_RESULTS_NO_HOSTNAME = {
    "hostName": None,
    "serialNumber": "FV-1KFS124000458",
    "operationMode": "Reverse Proxy",
    "haStatus": "Active-Passive",
    "firmwareVersion": "FortiWeb-1000F 7.2.12,build0437(GA),251023",
}

IFACE_RESULTS = [
    {"name": "mgmt1", "type": "physical", "ip": "192.168.1.99/24"},
    {"name": "port1", "type": "physical", "ip": "0.0.0.0/0"},
    {"name": "port2", "type": "physical", "ip": "0.0.0.0/0"},
    {"name": "vlan100", "type": "vlan", "ip": "10.0.0.1/24"},
]


def test_auth_header_shape():
    tok = fw._auth_header("netbox", "secret", "root")
    decoded = json.loads(base64.b64decode(tok).decode())
    assert decoded == {"username": "netbox", "password": "secret",
                       "vdom": "root"}


def test_parse_system_status():
    out = fw._parse_system_status(STATUS_RESULTS)
    assert out["name"] == "FortiWeb-Azadegan"
    assert out["serial"] == "FV-1KET122900020"
    assert out["model"] == "FortiWeb 1000E"
    assert out["version"] == "7.2.12"
    assert out["op_mode"] == "Reverse Proxy"
    assert out["ha_status"] == "Standalone"


def test_parse_system_status_unset_hostname():
    out = fw._parse_system_status(STATUS_RESULTS_NO_HOSTNAME)
    assert out["name"] is None                    # factory box -> None
    assert out["model"] == "FortiWeb 1000F"
    assert out["serial"] == "FV-1KFS124000458"
    assert out["ha_status"] == "Active-Passive"


def test_parse_interfaces_counts_physical_only():
    out = fw._parse_interfaces(IFACE_RESULTS)
    assert out["port_count"] == 3                 # mgmt1 + port1 + port2, not vlan


def test_parse_global_hostname():
    assert fw._parse_global_hostname({"hostname": "FortiWeb-Afranet"}) == "FortiWeb-Afranet"
    assert fw._parse_global_hostname({"hostname": ""}) is None
    assert fw._parse_global_hostname({}) is None


def test_session_get_returns_results_and_raises_on_errcode(monkeypatch):
    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self._payload

    sess = fw.FortiWebSession.__new__(fw.FortiWebSession)
    sess.timeout = 5
    sess.base = "https://x"
    sess.s = SimpleNamespace()

    # success path
    sess.s.get = lambda *a, **k: _Resp(200, {"results": {"serialNumber": "X"}})
    assert sess.get("/api/v2.0/system/status.systemstatus") == {"serialNumber": "X"}

    # error envelope
    sess.s.get = lambda *a, **k: _Resp(500, {"errcode": -1, "message": "bad"})
    with pytest.raises(RuntimeError):
        sess.get("/api/v2.0/cmdb/system/status")

    # auth rejection
    sess.s.get = lambda *a, **k: _Resp(401, {})
    with pytest.raises(fw.FortiWebAuthError):
        sess.get("/api/v2.0/system/status.systemstatus")


# ── NetBox ensure / offline ─────────────────────────────────────────────────

def _fake_devices_ep():
    from tests.test_netbox_sync import FakeEndpoint
    return FakeEndpoint()


def test_ensure_fortiweb_creates_and_matches_by_serial(monkeypatch):
    from tests.test_netbox_sync import FakeEndpoint
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: SimpleNamespace(dcim=SimpleNamespace(devices=devices_ep)))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)

    # find_device over the fake endpoint, matching by serial (role-agnostic —
    # the fake stores role as an int id, not a record with .name)
    def _find(serial, role_name=None):
        return next((d for d in devices_ep.items
                     if getattr(d, "serial", None) == serial), None)
    monkeypatch.setattr(nbx, "find_device", _find)

    probe = {"ip": "192.168.60.252", "serial": "FV-1KET122900020",
             "model": "FortiWeb 1000E", "hostname": "FortiWeb-Azadegan",
             "manufacturer": "Fortinet", "firmware": "7.2.12"}
    extra = {"op_mode": "Reverse Proxy", "ha_status": "Standalone",
             "port_count": 17}
    dev_id = nbx.ensure_fortiweb_device(probe, extra)

    payload = devices_ep.created[0]
    assert payload["name"] == "FortiWeb-Azadegan"
    assert payload["serial"] == "FV-1KET122900020"
    cf = payload["custom_fields"]
    assert cf["fortiweb_ip"] == "192.168.60.252"
    assert cf["fortiweb_enabled"] is True
    assert cf["fortiweb_model"] == "FortiWeb 1000E"
    assert cf["fortiweb_mode"] == "Reverse Proxy"
    assert cf["fortiweb_ha"] == "Standalone"
    assert cf["fortiweb_port_count"] == 17

    # second call with the same serial -> updates, never duplicates
    dev_id2 = nbx.ensure_fortiweb_device(probe, extra)
    assert dev_id2 == dev_id
    assert len(devices_ep.created) == 1


def test_ensure_fortiweb_hostname_fallback(monkeypatch):
    from tests.test_netbox_sync import FakeEndpoint
    devices_ep = FakeEndpoint()
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: SimpleNamespace(dcim=SimpleNamespace(devices=devices_ep)))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)

    # probe fallback hostname (factory box, hostName unset)
    probe = {"ip": "192.168.19.130", "serial": "FV-1KFS124000458",
             "model": "FortiWeb 1000F", "hostname": "fortiweb-192-168-19-130",
             "manufacturer": "Fortinet", "firmware": "7.2.12"}
    nbx.ensure_fortiweb_device(probe, {})
    assert devices_ep.created[0]["name"] == "fortiweb-192-168-19-130"


def test_mark_fortiweb_offline(monkeypatch):
    from tests.test_netbox_sync import FakeRecord, FakeEndpoint
    dev = FakeRecord(7, name="FortiWeb-Azadegan",
                     custom_fields={"fortiweb_enabled": True})
    devices_ep = FakeEndpoint([dev])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: SimpleNamespace(dcim=SimpleNamespace(devices=devices_ep)))

    nbx.mark_fortiweb_offline(7, "FortiWeb-Azadegan")
    assert devices_ep.updated[0]["status"] == "offline"
    assert devices_ep.updated[0]["custom_fields"]["fortiweb_enabled"] is False


def test_ensure_fortiweb_adopts_existing_serial_regardless_of_role(monkeypatch):
    """A device the AssetExplorer sync created (role Firewall, carrying
    asset_tag/department) must be ADOPTED by serial and its role corrected to
    WAF — not duplicated. Regression test for the Azadegan/Afranet dupes."""
    from tests.test_netbox_sync import FakeRecord, FakeEndpoint
    # pre-existing AE-created device: same serial, role Firewall, has asset data
    existing = FakeRecord(
        10228, name="Azadegan-FortiWeb", serial="FV-1KET122900020",
        site_id=13, role_id=99,                        # role_id 99 = Firewall
        role=SimpleNamespace(id=99, name="Firewall"),
        custom_fields={"ae_asset_id": "403", "ae_department": "ICT"})
    devices_ep = FakeEndpoint([existing])
    monkeypatch.setattr(nbx, "get_netbox",
                        lambda: SimpleNamespace(dcim=SimpleNamespace(devices=devices_ep)))
    monkeypatch.setattr(nbx, "get_or_create_manufacturer", lambda n: 11)
    monkeypatch.setattr(nbx, "get_or_create_role", lambda n, *a: 12)   # WAF id=12
    monkeypatch.setattr(nbx, "get_or_create_site", lambda n: 13)
    monkeypatch.setattr(nbx, "get_or_create_device_type", lambda *a, **k: 14)

    probe = {"ip": "192.168.60.252", "serial": "FV-1KET122900020",
             "model": "FortiWeb 1000E", "hostname": "FortiWeb-Azadegan",
             "manufacturer": "Fortinet", "firmware": "7.2.12"}
    dev_id = nbx.ensure_fortiweb_device(probe, {"op_mode": "Reverse Proxy"})

    # adopted, not duplicated
    assert dev_id == 10228
    assert len(devices_ep.created) == 0
    upd = devices_ep.updated[-1]
    assert upd["id"] == 10228
    assert upd["role"] == 12                            # role corrected to WAF
    assert upd["custom_fields"]["fortiweb_ip"] == "192.168.60.252"
    # AE fields preserved (update merges; we never clear them)
    assert upd["custom_fields"].get("ae_department") is None or \
           existing.custom_fields.get("ae_department") == "ICT"
