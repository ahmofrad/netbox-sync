"""Tests for the FortiGate REST API mappers (pure JSON -> dict)."""
import netbox_sync.collectors.fortigate as mod


STATUS_JSON = {"http_method": "GET", "results": {
    "hostname": "FGT-DC-01", "serial_number": "FGT60FTK21000001",
    "model": "FortiGate-60F", "version": "v7.2.4"}}

MONITOR_IFACES = {"results": {
    "port1": {"link": True, "speed": "1000full", "duplex": "full"},
    "port2": {"link": False, "speed": "1000", "duplex": "full"},
    "port1.10": {"link": True, "speed": "1000full"}}}

CMDB_IFACES = {"results": [
    {"name": "port1", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
    {"name": "port2", "type": "physical", "ip": "172.31.9.1 255.255.255.0"},
    {"name": "port1.10", "type": "vlan", "vlanid": 10,
     "interface": "port1", "ip": "10.10.10.1 255.255.255.0"},
]}


def test_fg_status():
    out = mod._fg_status(STATUS_JSON)
    assert out == {"hostname": "FGT-DC-01", "serial": "FGT60FTK21000001",
                   "model": "FortiGate-60F", "version": "v7.2.4"}


def test_fg_interfaces_merge():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["link"] is True
    assert ports["port1"]["speed_mbps"] == 1000
    assert ports["port2"]["link"] is False
    assert ports["port2"]["speed_mbps"] == 1000
    assert ports["port1.10"]["vlanid"] == 10
    assert ports["port1.10"]["parent"] == "port1"


def test_fg_vlans():
    vlans = mod._fg_vlans(CMDB_IFACES)
    assert vlans == [{"vid": 10, "name": "port1.10", "status": "active"}]


def test_fg_interface_type():
    assert mod._fg_interface_type(100) == "100base-tx"
    assert mod._fg_interface_type(1000) == "1000base-t"
    assert mod._fg_interface_type(10000) == "10gbase-t"
    assert mod._fg_interface_type(None) == "other"
