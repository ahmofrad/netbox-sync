"""NetBox-aware management command tests with no real Redis or subprocess."""
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from netbox_sync.worker import WorkerResult
from netbox_sync_plugin.management.commands import netbox_sync as command_module


class FakeState:
    schedule_enabled = True
    interval_minutes = 1440
    retention_runs = 3


class FakeService:
    instances = []

    def __init__(self):
        self.calls = []
        self.__class__.instances.append(self)

    def get_state(self):
        return FakeState()

    def execute_cli(self, **kwargs):
        self.calls.append(("run", kwargs))
        return WorkerResult("succeeded", True, summary="completed")

    def reconcile_schedule(self, state):
        self.calls.append(("schedule", state))
        return SimpleNamespace(pk=10)

    def cleanup(self, limit):
        self.calls.append(("cleanup", limit))
        return 4


@pytest.fixture(autouse=True)
def fake_service(monkeypatch):
    FakeService.instances = []
    monkeypatch.setattr(command_module, "SyncService", FakeService)
    monkeypatch.setattr(command_module.SyncState.objects, "get_or_create",
                        lambda key: (FakeState(), False))


def test_run_forwards_ordered_pipeline_options_and_status(capsys):
    call_command("netbox_sync", "run", "--skip-assetexplorer", "--lock-timeout", "0")
    service = FakeService.instances[-1]
    assert service.calls == [("run", {"include_assetexplorer": False, "lock_timeout": 0})]
    assert "completed" in capsys.readouterr().out


def test_schedule_and_cleanup_use_persistent_state(capsys):
    call_command("netbox_sync", "schedule")
    call_command("netbox_sync", "cleanup")
    service = FakeService.instances[0]
    service.calls.extend(FakeService.instances[1].calls)
    assert service.calls[0][0] == "schedule"
    assert service.calls[0][1].interval_minutes == 1440
    assert service.calls[1] == ("cleanup", 3)
    assert "scheduled" in capsys.readouterr().out.lower()


def test_failed_run_returns_nonzero():
    class FailedService(FakeService):
        def execute_cli(self, **kwargs):
            return WorkerResult("failed", False, summary="safe failure")
    command_module.SyncService = FailedService
    with pytest.raises(SystemExit) as error:
        call_command("netbox_sync", "run")
    assert error.value.code == 1
