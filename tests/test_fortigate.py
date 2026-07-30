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


LLDP_SUMMARY = """-----------------------------------------------------------------------------
Port        Device ID          SysName          Capabilities  TTL   Port ID
port1       00:1c:73:ab:cd:ef  F10-SW-W-02      B,R           120   Gi1/0/1
port2       00:1c:73:ab:cd:00  R4-Core-LAN-SW   B,R           120   Twe1/0/31
"""


def test_parse_lldp_summary():
    entries = mod._parse_lldp_summary(LLDP_SUMMARY)
    assert len(entries) == 2
    assert entries[0]["device_id"] == "F10-SW-W-02"
    assert entries[0]["local_intf"] == "port1"
    assert entries[0]["remote_intf"] == "Gi1/0/1"


TRANSCEIVERS = """Port 1  : SFP/SFP+ (10G)
   Vendor            : FINISAR CORP.
   Part Number       : FTLX8571D3BCL
   Serial Number     : ABC123456

Port 2  : SFP/SFP+ (10G)
   Vendor            : CISCO
   Part Number       : SFP-10G-SR
   Serial Number     : DEF789012
"""


def test_parse_transceivers():
    rows = mod._parse_transceivers(TRANSCEIVERS)
    assert len(rows) == 2
    assert rows[0]["vendor"] == "FINISAR CORP."
    assert rows[0]["part_number"] == "FTLX8571D3BCL"
    assert rows[0]["serial_number"] == "ABC123456"
    assert rows[1]["port"] == 2
