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
    {"name": "port1", "type": "physical", "ip": "0.0.0.0 0.0.0.0",
     "alias": "UPLINK-CORE"},
    {"name": "port2", "type": "physical", "ip": "172.31.9.1 255.255.255.0"},
    {"name": "port1.10", "type": "vlan", "vlanid": 10,
     "interface": "port1", "ip": "10.10.10.1 255.255.255.0"},
]}


def test_fg_status():
    out = mod._fg_status(STATUS_JSON)
    assert out == {"hostname": "FGT-DC-01", "serial": "FGT60FTK21000001",
                   "model": "FortiGate-60F", "version": "v7.2.4"}


# Real /monitor/system/status from a FortiGate 1800F running FortiOS 7.2.13:
# serial is TOP LEVEL, model is split into model_name/model_number.
STATUS_JSON_72 = {"http_method": "GET",
                  "serial": "FG180FTK21901250", "version": "v7.2.13",
                  "results": {"model_name": "FortiGate",
                              "model_number": "1800F", "model": "FG180F",
                              "hostname": "HQ",
                              "log_disk_status": "not_available"}}


def test_fg_status_fortios_72_real_shape():
    out = mod._fg_status(STATUS_JSON_72)
    assert out["serial"] == "FG180FTK21901250"
    assert out["model"] == "FortiGate 1800F"
    assert out["hostname"] == "HQ"
    assert out["version"] == "v7.2.13"


def test_fg_interfaces_merge():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["link"] is True
    assert ports["port1"]["speed_mbps"] == 1000
    assert ports["port2"]["link"] is False
    assert ports["port2"]["speed_mbps"] == 1000
    assert ports["port1.10"]["vlanid"] == 10
    assert ports["port1.10"]["parent"] == "port1"


def test_fg_interfaces_capture_alias():
    ports = {p["name"]: p for p in mod._fg_interfaces(MONITOR_IFACES, CMDB_IFACES)}
    assert ports["port1"]["alias"] == "UPLINK-CORE"
    assert ports["port2"]["alias"] == ""


def test_fg_interfaces_include_cmdb_vlan_subifs():
    """monitor/system/interface on FortiOS 7.2 reports ONLY physical ports —
    VLAN subinterfaces exist only in cmdb config and must be unioned in."""
    mon = {"results": {
        "port1": {"link": True, "speed": "1000full"},
        "port2": {"link": False, "speed": "1000"}}}
    cmdb = {"results": [
        {"name": "port1", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
        {"name": "port2", "type": "physical", "ip": "172.31.9.1 255.255.255.0"},
        {"name": "AP MGMT", "type": "vlan", "vlanid": 51,
         "interface": "Core Switch", "ip": "172.31.2.1 255.255.255.0"},
        {"name": "AsiaTech", "type": "vlan", "vlanid": 10,
         "interface": "PO5", "ip": "79.127.120.184 255.255.255.240"},
    ]}
    ports = {p["name"]: p for p in mod._fg_interfaces(mon, cmdb)}
    assert len(ports) == 4
    assert ports["AP MGMT"]["vlanid"] == 51
    assert ports["AP MGMT"]["parent"] == "Core Switch"
    assert ports["AP MGMT"]["link"] is True
    assert ports["AsiaTech"]["vlanid"] == 10


def test_fg_interfaces_include_aggregates_with_members():
    """Aggregates live only in cmdb (not in monitor) and must be added with
    their member lists for LAG linkage."""
    mon = {"results": {
        "port33": {"link": True, "speed": "10000full"},
        "port34": {"link": True, "speed": "10000full"}}}
    cmdb = {"results": [
        {"name": "port33", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
        {"name": "port34", "type": "physical", "ip": "0.0.0.0 0.0.0.0"},
        {"name": "Core Switch", "type": "aggregate",
         "member": [{"interface-name": "port33"},
                    {"interface-name": "port34"}]},
    ]}
    ports = {p["name"]: p for p in mod._fg_interfaces(mon, cmdb)}
    assert "Core Switch" in ports
    assert ports["Core Switch"]["type"] == "lag"
    assert ports["Core Switch"]["members"] == ["port33", "port34"]


def test_ssh_command_failure_detected():
    class FakeSess:
        def run(self, cmd):
            return "8757: Unknown action 0\nCommand fail. Return code -1"
    assert mod._ssh_run_or_none(FakeSess(), "diagnose lldp neighbor-summary",
                                "lldp") is None

    class GoodSess:
        def run(self, cmd):
            return "port1 00:1c:73:ab:cd:ef SW1 B,R 120 Gi1/0/1"
    assert mod._ssh_run_or_none(GoodSess(), "x", "lldp") is not None


IFCONFIG_A = """AP MGMT\tLink encap:Ethernet  HWaddr 00:09:0F:09:00:24
\tinet addr:172.31.2.1  Bcast:172.31.2.255  Mask:255.255.255.0

AsiaTech\tLink encap:Ethernet  HWaddr 00:09:0F:09:00:26
\tinet addr:79.127.120.184  Bcast:79.127.120.191  Mask:255.255.255.240
"""


def test_parse_ifconfig_a():
    out = mod._parse_ifconfig_a(IFCONFIG_A)
    assert out == {"AP MGMT": "00:09:0f:09:00:24",
                   "AsiaTech": "00:09:0f:09:00:26"}


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
