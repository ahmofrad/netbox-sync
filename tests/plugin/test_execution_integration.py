from types import SimpleNamespace

from netbox_sync.worker import WorkerResult


def test_execute_uses_supported_worker_arguments(monkeypatch):
    from netbox_sync_plugin.services import SyncService

    service = SyncService()
    captured = {}
    lock = SimpleNamespace()
    monkeypatch.setattr(service, "acquire_lock", lambda: lock)
    monkeypatch.setattr(service, "heartbeat", lambda: None)
    monkeypatch.setattr(service, "persist_result", lambda run, result: None)
    monkeypatch.setattr(service, "run_subprocess", lambda **kwargs: captured.update(kwargs) or WorkerResult("succeeded", True))
    run = SimpleNamespace(pk=12, job=None, assetexplorer_state="pending",
                          save=lambda **kwargs: None)
    result = service.execute(run)
    assert result.succeeded
    assert captured["lock"] is lock
    assert captured["run_id"] == 12
    assert "job" not in captured
