"""Focused NVR reliability regressions."""

from types import SimpleNamespace

from netbox_sync import report
from netbox_sync import sync as sync_module


def test_failed_collection_and_ensure_has_name_and_one_failure(monkeypatch):
    report.clear()
    monkeypatch.setattr(sync_module.time, "sleep", lambda _: None)
    sweep_names = []
    monkeypatch.setattr(
        sync_module,
        "_mark_nvr_cameras_offline_if_unreachable",
        lambda api, ip, name, family: sweep_names.append((ip, name, family)),
    )
    api = SimpleNamespace(dcim=SimpleNamespace(devices=SimpleNamespace(update=lambda values: True)))

    def fail_collection(ip):
        raise RuntimeError("collection failed")

    def fail_ensure(probe):
        raise ValueError("ensure failed")

    live = sync_module.process_nvrs(
        [{"ip": "192.0.2.10", "hostname": "nvr-one"}],
        fail_collection,
        fail_ensure,
        "Hikvision",
        {},
        {},
        api,
    )

    assert live == {"192.0.2.10"}
    assert sweep_names == [("192.0.2.10", "nvr-one", "Hikvision")]
    assert dict(report._grouped()) == {
        ("Hikvision NVR", "no data", "RuntimeError: collection failed"): ["192.0.2.10"]
    }
    report.clear()
