"""JobRunner registration and history outcome tests."""
import sys
from types import ModuleType, SimpleNamespace

import pytest

from netbox_sync.worker import WorkerResult
from netbox_sync_plugin.jobs import SyncPipelineJob


def test_job_name_and_dedicated_queue():
    assert SyncPipelineJob.name == "NetBox synchronization"
    assert SyncPipelineJob.queue_name == "netbox_sync_plugin.sync"
    assert SyncPipelineJob.job_timeout >= 86400


def test_plugin_does_not_register_decorated_rq_compatibility_job():
    from pathlib import Path
    source = (Path(__file__).parents[2] / "netbox_sync_plugin" / "jobs.py").read_text()
    assert "run_pipeline_subprocess" not in source
    assert "@rq_job" not in source


def test_plugin_does_not_register_unanchored_system_job(monkeypatch):
    from netbox_sync_plugin import NetBoxSyncConfig
    registry = {"system_jobs": {}}
    monkeypatch.setitem(sys.modules, "netbox.registry", SimpleNamespace(registry=registry))
    monkeypatch.setattr(NetBoxSyncConfig, "__path__", [], raising=False)
    config = NetBoxSyncConfig.__new__(NetBoxSyncConfig)
    config.ready()
    assert registry["system_jobs"] == {}


def test_unexpected_job_error_is_sanitized_before_netbox_persistence():
    terminated = []
    job = SimpleNamespace(
        interval=None, object=None,
        start=lambda: None,
        terminate=lambda **kwargs: terminated.append(kwargs),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(SyncPipelineJob, "run", lambda self, **kwargs: (_ for _ in ()).throw(
        RuntimeError("request failed Authorization: Token fake-job-canary")))
    try:
        SyncPipelineJob.handle(job)
    finally:
        monkeypatch.undo()
    assert terminated[0]["status"] == "errored"
    assert "fake-job-canary" not in terminated[0]["error"]
    assert "[redacted]" in terminated[0]["error"]
    assert "RuntimeError" in terminated[0]["error"]


def test_recurring_handle_preserves_dedicated_queue_timeout_and_arguments():
    from django.utils import timezone
    calls = []
    job = SimpleNamespace(
        interval=60, scheduled=timezone.now(), started=None, object=SimpleNamespace(pk=1),
        name="NetBox synchronization", user=None,
        start=lambda: None, terminate=lambda **kwargs: None,
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(SyncPipelineJob, "enqueue", classmethod(
        lambda cls, **kwargs: calls.append(kwargs)), raising=False)
    monkeypatch.setattr(SyncPipelineJob, "run", lambda self, **kwargs: None)
    try:
        SyncPipelineJob.handle(job, include_assetexplorer=False)
    finally:
        monkeypatch.undo()
    assert calls[0]["queue_name"] == "netbox_sync_plugin.sync"
    assert calls[0]["job_timeout"] == 43200
    assert calls[0]["include_assetexplorer"] is False


def test_job_persists_failed_ownership_loss(monkeypatch):
    persisted = []
    result = WorkerResult("failed", False, summary="Execution lock ownership was lost")
    service = SimpleNamespace(execute=lambda run: result)
    job = SyncPipelineJob.__new__(SyncPipelineJob)
    job.service = service
    run = SimpleNamespace(pk=1, trigger="scheduled", finish=lambda status, summary: persisted.append((status, summary)))
    job.execute_run = lambda **kwargs: run
    try:
        job.run()
    except Exception as exc:
        assert type(exc).__name__ == "JobFailed"
    else:
        raise AssertionError("expected JobFailed")
    assert persisted[0][0] == "failed"
    assert "ownership" in persisted[0][1]
