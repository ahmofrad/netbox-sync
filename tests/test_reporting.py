"""Structured per-run reporting and credential-redaction tests."""
import ast
import threading
from pathlib import Path
from types import SimpleNamespace

from netbox_sync import report
from netbox_sync.netbox import ReportingDevices
from tests.test_netbox_sync import FakeEndpoint, FakeRecord


def test_reporter_aggregates_device_outcomes_and_families():
    reporter = report.RunReporter()
    reporter.start_family("servers", "Servers")
    reporter.record_device_outcome("created", 1, family="servers")
    reporter.record_device_outcome("updated", 1, family="servers")
    reporter.record_device_outcome("unchanged", 1, family="servers")
    reporter.record_adoption(1, family="servers")
    reporter.finish_family("servers")

    snapshot = reporter.snapshot()
    assert snapshot["counters"] == {
        "discovered": 0, "created": 1, "updated": 1, "adopted": 1,
        "unchanged": 1, "failed": 0, "offline": 0,
    }
    assert snapshot["families"]["servers"]["counters"]["created"] == 1
    assert snapshot["families"]["servers"]["status"] == "succeeded"


def test_failure_recording_is_thread_safe_and_bounded():
    reporter = report.RunReporter(max_issues=50)
    def record():
        for _ in range(50):
            reporter.record_processing_failure("servers", "192.0.2.1", "failed", count_attempt=True)
    threads = [threading.Thread(target=record) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snapshot = reporter.snapshot()
    assert snapshot["counters"]["failed"] == 200
    assert snapshot["issues"] == [{
        "family": "servers", "source": "192.0.2.1", "category": "no data",
        "severity": "error", "message": "failed", "occurrences": 200,
    }]


def test_reporter_clears_between_runs_and_sanitizes_text():
    reporter = report.RunReporter(max_message_length=100)
    reporter.record_processing_failure(
        "servers", "192.0.2.1",
        "request failed token=fake-canary-value password: fake-password-value "
        "https://user:fake-url-password@example.invalid/path",
    )
    text = str(reporter.snapshot())
    assert "fake-canary-value" not in text
    assert "fake-password-value" not in text
    assert "fake-url-password" not in text
    reporter.clear()
    assert reporter.snapshot()["counters"]["failed"] == 0
    assert reporter.snapshot()["issues"] == []


def test_family_failure_recording_precedes_loop_continuation():
    tree = ast.parse(Path("netbox_sync/sync.py").read_text())
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for index, statement in enumerate(node.body[:-1]):
            if isinstance(statement, (ast.Continue, ast.Break, ast.Return, ast.Raise)):
                if any(isinstance(later, ast.Expr) and isinstance(later.value, ast.Call)
                       and isinstance(later.value.func, ast.Name)
                       and later.value.func.id == "record_processing_failure"
                       for later in node.body[index + 1:]):
                    violations.append(statement.lineno)
    assert violations == []


import pytest


@pytest.mark.parametrize("label,family", [
    ("Server", "servers"),
    ("Storage", "storage"),
    ("SAN switch", "san_switches"),
    ("Cisco switch", "cisco_switches"),
    ("FortiGate", "fortigates"),
    ("FortiWeb", "fortiwebs"),
    ("FTD", "ftds"),
    ("Ruckus", "ruckus"),
    ("UniFi console", "unifi"),
    ("Hikvision NVR", "hikvision_nvrs"),
    ("Dahua NVR", "dahua_nvrs"),
    ("Uniview NVR", "unv_nvrs"),
    ("UNV NVR", "unv_nvrs"),
    ("Offline sweep", "offline_sweeps"),
])
def test_probe_failures_use_canonical_family_keys(label, family):
    reporter = report.RunReporter()
    reporter.start_family(family)
    reporter.record_probe_failure(label, "192.0.2.1", "unreachable", "port closed")
    reporter.finish_family(family)
    snapshot = reporter.snapshot()
    assert snapshot["families"][family]["counters"]["failed"] == 1
    assert snapshot["families"][family]["status"] == "failed"
    assert snapshot["issues"][0]["family"] == family


def test_bootstrap_failure_survives_report_initialization(monkeypatch):
    from netbox_sync import sync
    reporter = report.RunReporter()
    monkeypatch.setattr(sync, "ensure_custom_fields",
                        lambda: (_ for _ in ()).throw(RuntimeError("bootstrap failed")))
    monkeypatch.setattr(sync, "get_reporter", lambda: reporter)
    monkeypatch.setattr(report, "get_reporter", lambda: reporter)
    monkeypatch.setattr(sync, "scan_all", lambda: (_ for _ in ()).throw(RuntimeError("stop discovery")))
    with pytest.raises(RuntimeError, match="stop discovery"):
        sync.run_sync()
    snapshot = reporter.snapshot()
    assert snapshot["issues"][0]["family"] == "bootstrap"
    assert snapshot["counters"]["failed"] == 1


def test_family_finishes_after_reconciliation_and_reflects_write_failure():
    reporter = report.RunReporter()
    reporter.start_family("servers")
    reporter.record_device_outcome("discovered", family="servers")
    assert reporter.snapshot()["families"]["servers"]["status"] == "running"
    reporter.record_processing_failure("servers", "192.0.2.1", "write failed")
    reporter.finish_family("servers")
    assert reporter.snapshot()["families"]["servers"]["status"] == "failed"


def test_discovery_success_followed_by_failed_write_finishes_family_failed(monkeypatch):
    from netbox_sync import sync
    reporter = report.RunReporter()
    monkeypatch.setattr(sync, "ensure_custom_fields", lambda: None)
    monkeypatch.setattr(sync, "clear_scan_report", lambda: None)
    monkeypatch.setattr(sync, "get_reporter", lambda: reporter)
    monkeypatch.setattr(report, "get_reporter", lambda: reporter)
    monkeypatch.setattr(sync, "scan_all", lambda: {key: [] for key in (
        "servers", "storage", "san_switches", "cisco_switches", "fortigates",
        "fortiwebs", "ftds", "ruckus", "unifi", "hikvision_nvrs",
        "dahua_nvrs", "unv_nvrs")} | {
        "servers": [{"ip": "192.0.2.1", "host": "server-one"}]})
    monkeypatch.setattr(sync, "get_netbox", lambda: SimpleNamespace())
    monkeypatch.setattr(sync, "ensure_server_device",
                        lambda probe: (_ for _ in ()).throw(RuntimeError("write failed")))
    monkeypatch.setattr(sync, "_power_sync_pass", lambda: None)
    monkeypatch.setattr(sync, "ensure_custom_fields_if_set", lambda: None)
    monkeypatch.setattr(sync, "sync_parent_prefixes", lambda: {})
    monkeypatch.setattr(sync, "sweep_stale_parents", lambda: None)
    monkeypatch.setattr(sync, "_offline_sweep", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, "print_summary", lambda found: None)
    sync.run_sync()
    family = reporter.snapshot()["families"]["servers"]
    assert family["counters"]["discovered"] == 1
    assert family["status"] == "failed"


def test_devices_proxy_reports_create_changed_update_and_noop():
    current = FakeRecord(1, name="server-one", status="active", custom_fields={"bmc_ip": "192.0.2.1"})
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    devices = ReportingDevices(raw, reporter)

    devices.create({"name": "server-two", "status": "active"})
    devices.update([{"id": 1, "name": "server-one", "status": "active",
                     "custom_fields": {"bmc_ip": "192.0.2.1"}}], current=current)
    devices.update([{"id": 1, "name": "server-renamed"}], current=current)

    assert raw.created == [{"name": "server-two", "status": "active"}]
    assert raw.updated[-1] == {"id": 1, "name": "server-renamed"}
    assert reporter.snapshot()["counters"]["created"] == 1
    assert reporter.snapshot()["counters"]["unchanged"] == 1
    assert reporter.snapshot()["counters"]["updated"] == 1


def test_real_recordset_is_materialized_once_and_replayable(monkeypatch):
    from pynetbox.core.response import RecordSet
    from netbox_sync import netbox
    endpoint = SimpleNamespace(
        api=None,
        return_obj=lambda values, api, endpoint: SimpleNamespace(**values),
    )
    request = SimpleNamespace(count=1, get=lambda: iter([{"id": 1, "serial": "SERIAL-ONE"}]))
    raw = SimpleNamespace(filter=lambda **kwargs: iter(RecordSet(endpoint, request)))
    reporter = report.RunReporter()
    devices = ReportingDevices(raw, reporter)
    monkeypatch.setattr(netbox, "get_netbox", lambda: SimpleNamespace(
        dcim=SimpleNamespace(devices=devices)))
    found = netbox.find_device("SERIAL-ONE")
    assert found.id == 1
    assert len(devices.filter(serial="SERIAL-ONE")) == 1


def test_real_record_shapes_compare_choice_and_relation_values(monkeypatch):
    from pynetbox.core.response import Record
    current = Record({
        "id": 1, "status": {"value": "active", "label": "Active"},
        "role": {"id": 2, "name": "Server"},
        "site": {"id": 3, "name": "HQ"},
        "device_type": {"id": 4, "model": "Server"},
        "custom_fields": {"bmc_ip": "192.0.2.1", "preserved": "yes"},
    }, None, None)
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    ReportingDevices(raw, reporter).update([{
        "id": 1, "status": "active", "role": 2, "site": 3, "device_type": 4,
        "custom_fields": {"bmc_ip": "192.0.2.1"},
    }], current=current)
    assert reporter.snapshot()["counters"]["unchanged"] == 1
    assert reporter.snapshot()["counters"]["updated"] == 0


def test_devices_proxy_uses_existing_reads_and_compares_partial_custom_fields():
    current = FakeRecord(1, name="server-one", custom_fields={"bmc_ip": "old", "site_note": "keep"})
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    devices = ReportingDevices(raw, reporter)
    assert devices.get(id=1) is current
    devices.update([{"id": 1, "custom_fields": {"bmc_ip": "old"}}])
    devices.update([{"id": 1, "custom_fields": {"bmc_ip": "new"}}])
    counters = reporter.snapshot()["counters"]
    assert counters["unchanged"] == 1
    assert counters["updated"] == 1


def test_scan_miss_bookkeeping_is_not_a_reconciliation_change(monkeypatch):
    from netbox_sync import netbox
    current = FakeRecord(1, custom_fields={"scan_misses": 0})
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    monkeypatch.setattr(netbox, "get_netbox", lambda: SimpleNamespace(
        dcim=SimpleNamespace(devices=ReportingDevices(raw, reporter))))
    assert netbox._persist_scan_misses(current, 1)
    assert reporter.snapshot()["counters"]["updated"] == 0


def test_same_role_serial_match_is_not_counted_as_adoption(monkeypatch):
    from netbox_sync import netbox
    current = FakeRecord(1, name="server-one", serial="SERIAL-ONE",
                         role=SimpleNamespace(name="Server"),
                         custom_fields={"bmc_ip": "192.0.2.1"})
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    monkeypatch.setattr(netbox, "get_netbox", lambda: SimpleNamespace(
        dcim=SimpleNamespace(devices=ReportingDevices(raw, reporter))))
    monkeypatch.setattr(report, "get_reporter", lambda: reporter)
    monkeypatch.setattr(netbox, "get_or_create_manufacturer", lambda name: 1)
    monkeypatch.setattr(netbox, "get_or_create_role", lambda *args: 2)
    monkeypatch.setattr(netbox, "get_or_create_site", lambda name: 3)
    monkeypatch.setattr(netbox, "get_or_create_device_type", lambda *args: 4)
    netbox._ensure_device(
        probe={"ip": "192.0.2.1", "hostname": "server-one", "serial": "SERIAL-ONE"},
        manufacturer="HPE", role_name="Server", prefix="server",
        cf={"bmc_ip": "192.0.2.1"}, adopt_any_role=True)
    assert reporter.snapshot()["counters"]["adopted"] == 0


@pytest.mark.parametrize("name,ip", [
    ("renamed-server", "192.0.2.1"),
    ("server-one", "192.0.2.99"),
])
def test_same_role_blank_serial_fallback_is_update_not_adoption(monkeypatch, name, ip):
    from netbox_sync import netbox
    current = FakeRecord(1, name=name, serial="", role_id=2, site_id=99,
                         role=SimpleNamespace(name="Server"),
                         custom_fields={"bmc_ip": ip})
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    monkeypatch.setattr(netbox, "get_netbox", lambda: SimpleNamespace(
        dcim=SimpleNamespace(devices=ReportingDevices(raw, reporter))))
    monkeypatch.setattr(report, "get_reporter", lambda: reporter)
    monkeypatch.setattr(netbox, "get_or_create_manufacturer", lambda name: 1)
    monkeypatch.setattr(netbox, "get_or_create_role", lambda *args: 2)
    monkeypatch.setattr(netbox, "resolve_site", lambda *args: "HQ")
    monkeypatch.setattr(netbox, "get_or_create_site", lambda name: 3)
    monkeypatch.setattr(netbox, "get_or_create_device_type", lambda *args: 4)
    netbox.ensure_server_device({"ip": "192.0.2.1", "hostname": "server-one",
                                 "serial": "", "model": "Server"})
    counters = reporter.snapshot()["counters"]
    assert raw.create_calls == 0
    assert counters["updated"] == 1
    assert counters["adopted"] == 0


def test_cross_role_name_fallback_counts_as_adoption(monkeypatch):
    from netbox_sync import netbox
    current = FakeRecord(1, name="ftd-one", serial="OTHER-SERIAL",
                         role=SimpleNamespace(name="AssetExplorer"), site_id=3,
                         custom_fields={})
    raw = FakeEndpoint([current])
    reporter = report.RunReporter()
    monkeypatch.setattr(netbox, "get_netbox", lambda: SimpleNamespace(
        dcim=SimpleNamespace(devices=ReportingDevices(raw, reporter))))
    monkeypatch.setattr(report, "get_reporter", lambda: reporter)
    monkeypatch.setattr(netbox, "get_or_create_manufacturer", lambda name: 1)
    monkeypatch.setattr(netbox, "get_or_create_role", lambda *args: 2)
    monkeypatch.setattr(netbox, "get_or_create_site", lambda name: 3)
    monkeypatch.setattr(netbox, "get_or_create_device_type", lambda *args: 4)
    monkeypatch.setattr(netbox, "find_device", lambda *args, **kwargs: None)
    netbox._ensure_device(
        probe={"ip": "192.0.2.1", "hostname": "ftd-one", "serial": "FTD-SERIAL"},
        manufacturer="Cisco", role_name="FTD", prefix="ftd",
        cf={"ftd_ip": "192.0.2.1"}, adopt_any_role=True, name_any_role=True)
    assert reporter.snapshot()["counters"]["adopted"] == 1


def test_devices_proxy_preserves_update_exceptions_and_normal_attributes():
    class BrokenEndpoint:
        custom_attribute = "preserved"
        def update(self, values):
            raise ValueError("write failed")
        def get(self, **kwargs):
            raise AssertionError("reporting must not issue pre-update reads")
        def filter(self, **kwargs):
            raise AssertionError("reporting must not issue pre-update reads")
    reporter = report.RunReporter()
    devices = ReportingDevices(BrokenEndpoint(), reporter)
    try:
        devices.update([{"id": 1, "status": "active"}])
    except ValueError as exc:
        assert str(exc) == "write failed"
    else:
        raise AssertionError("update exception was swallowed")
    assert devices.custom_attribute == "preserved"
    assert reporter.snapshot()["counters"]["updated"] == 0


def test_device_proxy_compares_dictionary_and_pynetbox_like_records():
    raw = SimpleNamespace(
        updates=[],
        get=lambda **kwargs: {"id": 1, "name": "same", "custom_fields": {"scan_misses": 0}},
        update=lambda values: None,
    )
    reporter = report.RunReporter()
    ReportingDevices(raw, reporter).update([{"id": 1, "name": "same"}],
                                           current=raw.get(id=1))
    assert reporter.snapshot()["counters"]["unchanged"] == 1
