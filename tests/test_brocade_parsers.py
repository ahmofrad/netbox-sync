"""Tests for the Brocade Fabric OS CLI output parsers."""
import netbox_sync.collectors.brocade as mod


SWITCHSHOW = """switchName:     SW-DC1-01
switchType:     170.9
switchState:    Online
switchMode:     Native
switchRole:     Principal
switchDomain:   1
switchId:       fffc01
switchWwn:      10:00:50:eb:1a:4f:3e:80
zoning:         OFF
switchBeacon:   OFF

Index Port Address Media Speed State     Proto
==============================================
   0   0   010000   id  N16   Online    FC  F-Port  50:06:0e:80:11:22:33:44
   1   1   010100   id  N16   No_Light  FC
   2   2   010200   cu  N8    Online    FC  E-Port  10:00:00:11:22:33:44:55 "upstream"
"""


def test_parse_switchshow_headers():
    headers, ports = mod._parse_switchshow(SWITCHSHOW)
    assert headers["switchname"] == "SW-DC1-01"
    assert headers["switchtype"] == "170.9"
    assert headers["switchwwn"] == "10:00:50:eb:1a:4f:3e:80"
    assert headers["switchstate"] == "Online"


def test_parse_switchshow_ports():
    headers, ports = mod._parse_switchshow(SWITCHSHOW)
    assert len(ports) == 3

    p0 = ports[0]
    assert p0["index"] == 0
    assert p0["port"] == 0
    assert p0["address"] == "010000"
    assert p0["speed"] == "N16"
    assert p0["state"] == "Online"
    assert p0["proto"] == "FC"
    assert "50:06:0e:80:11:22:33:44" in p0["comment"]

    p1 = ports[1]
    assert p1["port"] == 1
    assert p1["state"] == "No_Light"

    p2 = ports[2]
    assert p2["speed"] == "N8"
    assert "E-Port" in p2["comment"]


def test_parse_version():
    text = (
        "Kernel:     2.6.14.2\n"
        "Fabric OS:  v9.1.1b\n"
        "Made on:    Thu Apr 20 19:02:15 2023\n"
        "Flash:      Mon Jan  1 00:00:00 2024\n"
        "BootProm:   1.0.9\n"
    )
    out = mod._parse_version(text)
    assert out["fabric_os"] == "v9.1.1b"
    assert out["kernel"] == "2.6.14.2"


def test_parse_nsshow_multiple_entries():
    text = (
        "Port Id: 010c00\n"
        "World Wide Port Name: 50:06:0e:80:11:22:33:44\n"
        "World Wide Node Name: 60:06:0e:80:11:22:33:44\n"
        "Port Id: 010d00\n"
        "World Wide Port Name: 50:06:0e:80:11:22:33:55\n"
        "World Wide Node Name: 60:06:0e:80:11:22:33:55\n"
    )
    entries = mod._parse_nsshow(text)
    assert len(entries) == 2
    assert entries[0]["port_id"] == "010c00"
    assert entries[0]["world_wide_port_name"] == "50:06:0e:80:11:22:33:44"
    assert entries[1]["port_id"] == "010d00"


def test_parse_sfpshow_compact_format():
    text = (
        "Port  0: id (sw) Vendor: BROCADE  Serial No: HAA213456789012  Speed: 4,8,16_Gbps\n"
        "Port  1: id (sw) Vendor: FINISAR  Serial No: FNS1234567  Speed: 4,8,16_Gbps\n"
    )
    rows = mod._parse_sfpshow(text)
    assert len(rows) == 2
    assert rows[0]["port"] == 0
    assert rows[0]["vendor"] == "BROCADE"
    assert rows[0]["serial_no"] == "HAA213456789012"
    assert rows[1]["vendor"] == "FINISAR"


def test_parse_sfpshow_detailed_format():
    text = (
        "Port  0:\n"
        "Identifier:  3    SFP\n"
        "Connector:   7    LC\n"
        "Vendor: BROCADE\n"
        "Vendor Part Number: 57-1000487-01\n"
        "Vendor Serial Number: HAA213456789012\n"
        "Port  1:\n"
        "Identifier:  3    SFP\n"
        "Vendor: FINISAR\n"
        "Vendor Part Number: FTLF8529P3BCV-1B\n"
        "Vendor Serial Number: FNS7654321\n"
    )
    rows = mod._parse_sfpshow(text)
    assert len(rows) == 2
    assert rows[0]["vendor"] == "BROCADE"
    assert rows[0]["vendor_serial_number"] == "HAA213456789012"
    assert rows[0]["vendor_part_number"] == "57-1000487-01"
    assert rows[1]["vendor_serial_number"] == "FNS7654321"


def test_parse_chassisshow():
    text = (
        "Chassis PID: BES-6510\n"
        "Chassis Serial No: BRC123456789\n"
        "Supplier Part Num: XBR-000192\n"
    )
    out = mod._parse_chassisshow(text)
    assert out["chassis_pid"] == "BES-6510"
    assert out["chassis_serial_no"] == "BRC123456789"
    assert out["supplier_part_num"] == "XBR-000192"


def test_wwn_normalize():
    assert mod._wwn_normalize("10:00:50:EB:1A:4F:3E:80") == "10:00:50:eb:1a:4f:3e:80"
    assert mod._wwn_normalize("100050eb1a4f3e80") == "10:00:50:eb:1a:4f:3e:80"
    assert mod._wwn_normalize("not-a-wwn") is None
    assert mod._wwn_normalize(None) is None


def test_strip_prompt_removes_echo_and_prompt():
    raw = "switchshow\nline one\nline two\nadmin@sw1:>\n"
    out = mod.BrocadeSwitchSession._strip_prompt(raw, "switchshow")
    assert out == "line one\nline two"
