"""Tests for the Hikvision ISAPI XML parsers."""
import netbox_sync.collectors.hikvision as mod


DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceName>CAM-12</deviceName>
  <model>DS-2CD2143G2-I</model>
  <serialNumber>ABCD123456789</serialNumber>
  <macAddress>b4:0b:44:12:ab:cd</macAddress>
  <firmwareVersion>V5.7.3</firmwareVersion>
  <deviceType>IPCamera</deviceType>
  <channelID>12</channelID>
</DeviceInfo>
"""


def test_parse_device_info_namespaced_with_channel():
    out = mod._parse_device_info(DEVICE_INFO)
    assert out["name"] == "CAM-12"
    assert out["model"] == "DS-2CD2143G2-I"
    assert out["serial"] == "ABCD123456789"
    assert out["mac"] == "b4:0b:44:12:ab:cd"
    assert out["firmware"] == "V5.7.3"
    assert out["channel"] == "12"
