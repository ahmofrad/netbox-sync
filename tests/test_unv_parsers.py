"""Tests for the Uniview (UNV) LAPI parsers (JSON envelopes)."""
import json

import pytest

import netbox_sync.collectors.unv as mod


DEVICE_INFO = json.dumps({"Response": {
    "ResponseURL": "/LAPI/V1.0/System/DeviceInfo",
    "ResponseCode": 0, "ResponseString": "Succeed", "StatusCode": 0,
    "Data": {"ID": 0, "DeviceName": "NVR302-16S2", "DeviceType": 1,
             "DeviceModel": "NVR302-16S2", "SerialNumber": "210235XAX93239000081",
             "FirmwareVersion": "NVR-B3601.37.45.230825"}}})

CHANNEL_DETAILS = json.dumps({"Response": {
    "ResponseCode": 0, "ResponseString": "Succeed", "StatusCode": 0,
    "Data": {"Nums": 2, "DetailInfos": [
        {"ID": 1, "Name": "Kitchen", "Status": 1, "Manufacturer": "HIKVISION",
         "DeviceModel": "DS-2CD1131-I",
         "AddressInfo": {"Address": "192.168.112.67", "Port": 80,
                         "MAC": "f8:4d:fc:58:ef:c2"}},
        {"ID": 2, "Name": "", "Status": 0, "Manufacturer": "",
         "DeviceModel": "",
         "AddressInfo": {"Address": "192.168.112.68", "Port": 80, "MAC": ""}},
    ]}}})

IPC_DEVICE_INFOS = json.dumps({"Response": {
    "ResponseCode": 0, "ResponseString": "Succeed", "StatusCode": 0,
    "Data": {"Nums": 2, "DeviceInfos": [
        {"ID": 1, "DeviceModel": "DS-2CD1131-I",
         "SerialNumber": "DS-2CD1131-I20181219AAWRC79938008",
         "FirmwareVersion": "V5.5.54 build 180821"},
        {"ID": 2, "DeviceModel": "",
         "SerialNumber": "", "FirmwareVersion": ""},
    ]}}})

ERROR_ENVELOPE = json.dumps({"Response": {
    "ResponseCode": 1, "ResponseString": "Common Error", "StatusCode": 65535,
    "Data": {}}})


def test_lapi_data_unwraps_success():
    data = mod._lapi_data(DEVICE_INFO)
    assert data["DeviceModel"] == "NVR302-16S2"


def test_lapi_data_raises_on_error_envelope():
    with pytest.raises(RuntimeError):
        mod._lapi_data(ERROR_ENVELOPE)


def test_parse_device_info():
    out = mod._parse_device_info(DEVICE_INFO)
    assert out == {"name": "NVR302-16S2", "model": "NVR302-16S2",
                   "serial": "210235XAX93239000081",
                   "firmware": "NVR-B3601.37.45.230825"}


def test_parse_channel_details():
    cams = mod._parse_channel_details(CHANNEL_DETAILS)
    assert len(cams) == 2
    c1, c2 = cams
    assert c1["channel"] == 1
    assert c1["name"] == "Kitchen"
    assert c1["online"] is True
    assert c1["manufacturer"] == "Hikvision"    # HIKVISION -> capitalize()
    assert c1["model"] == "DS-2CD1131-I"
    assert c1["ip"] == "192.168.112.67"
    assert c1["mac"] == "f8:4d:fc:58:ef:c2"
    assert c2["online"] is False                # Status 0
    assert c2["name"] is None                   # empty -> None (collector fills)
    assert c2["mac"] is None
    assert c2["manufacturer"] == "Uniview"      # empty mfr default


def test_parse_ipc_device_infos():
    out = mod._parse_ipc_device_infos(IPC_DEVICE_INFOS)
    assert out[1]["serial"] == "DS-2CD1131-I20181219AAWRC79938008"
    assert out[1]["firmware"] == "V5.5.54 build 180821"
    assert out[2]["serial"] is None             # empty -> None


def test_norm_mac():
    assert mod._norm_mac("4C:BD:8F:6B:01:FA") == "4c:bd:8f:6b:01:fa"
    assert mod._norm_mac("garbage") is None
    assert mod._norm_mac(None) is None

# ── UNV empty-channel filtering (collect-level) ─────────────────────────────

def test_unv_collect_skips_empty_channel_slots(monkeypatch):
    import netbox_sync.collectors.unv as unv

    class _FakeSession:
        def __init__(self, ip, port=None, timeout=15): pass
        def get(self, path):
            if path == "/LAPI/V1.0/System/DeviceInfo":
                return {"DeviceName": "NVR302-16S2", "DeviceModel": "NVR302-16S2",
                        "SerialNumber": "SN", "FirmwareVersion": "FW"}
            if path == "/LAPI/V1.0/Channels/System/ChannelDetailInfos":
                return {"DetailInfos": [
                    {"ID": 1, "Name": "Kitchen", "Status": 1,
                     "Manufacturer": "HIKVISION", "DeviceModel": "DS-2CD1131-I",
                     "AddressInfo": {"Address": "192.168.112.67",
                                     "MAC": "f8:4d:fc:58:ef:c2"}},
                    {"ID": 2, "Name": "IP Camera 2", "Status": 0,
                     "Manufacturer": "", "DeviceModel": "",
                     "AddressInfo": {"Address": "", "MAC": ""}},
                ]}
            if path == "/LAPI/V1.0/Channels/System/DeviceInfos":
                return {"DeviceInfos": [
                    {"ID": 1, "DeviceModel": "DS-2CD1131-I",
                     "SerialNumber": "SER1", "FirmwareVersion": "FW1"},
                ]}
            raise RuntimeError(path)
        def logout(self): pass

    monkeypatch.setattr(unv, "UnvSession", _FakeSession)
    out = unv.unv_collect("192.168.112.66")
    assert len(out["cameras"]) == 1          # the empty slot is dropped
    cam = out["cameras"][0]
    assert cam["serial"] == "SER1"
    assert cam["mac"] == "f8:4d:fc:58:ef:c2"
