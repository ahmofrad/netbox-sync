"""Scheduling, deduplication, and safe execution service tests."""
from types import SimpleNamespace

import pytest

from netbox_sync.locking import RedisLockError
from netbox_sync_plugin.services import SyncService


class JobRunner:
    calls = []
    jobs = []
    @classmethod
    def enqueue_once(cls, **kwargs):
        cls.calls.append(("once", kwargs))
        job = SimpleNamespace(**kwargs, pk=len(cls.calls), delete=lambda: None)
        cls.jobs.append(job)
        return job
    @classmethod
    def enqueue(cls, **kwargs):
        cls.calls.append(("immediate", kwargs))
        job = SimpleNamespace(**kwargs, pk=len(cls.calls), delete=lambda: None)
        cls.jobs.append(job)
        return job
    @classmethod
    def get_jobs(cls, instance):
        return cls.jobs


@pytest.fixture(autouse=True)
def clear_job_runner():
    JobRunner.calls = []
    JobRunner.jobs = []


@pytest.fixture(autouse=True)
def disable_postgres_advisory_lock(monkeypatch):
    import netbox_sync_plugin.services as services
    monkeypatch.setattr(services, "_scheduling_lock", lambda: services.nullcontext())


class JobQuerySet:
    def __init__(self, jobs):
        self.jobs = list(jobs)

    def filter(self, **criteria):
        def matches(job):
            for key, expected in criteria.items():
                actual = getattr(job, key.split("__", 1)[0])
                if key == "status__in" and actual not in expected:
                    return False
                if key == "interval__isnull" and (actual is None) != expected:
                    return False
            return True
        return JobQuerySet(job for job in self.jobs if matches(job))

    def first(self):
        return self.jobs[0] if self.jobs else None

    def __iter__(self):
        return iter(self.jobs)


class SourceFaithfulJobRunner(JobRunner):
    @classmethod
    def enqueue(cls, **kwargs):
        cls.calls.append(("immediate", kwargs))
        job = SimpleNamespace(**kwargs, pk=len(cls.calls), status="pending",
                              deleted=False)
        job.delete = lambda: setattr(job, "deleted", True)
        cls.jobs.append(job)
        return job

    @classmethod
    def get_jobs(cls, instance):
        return JobQuerySet(job for job in cls.jobs if job.instance is instance)

    @classmethod
    def enqueue_once(cls, **kwargs):
        existing = cls.get_jobs(kwargs["instance"]).filter(
            status__in=("pending", "scheduled", "running")).first()
        if existing:
            if existing.interval == kwargs["interval"]:
                return existing
            existing.delete()
        return cls.enqueue(**kwargs)


def test_schedule_disabled_cancels_anchored_future_jobs_without_enqueue():
    deleted = []
    future = SimpleNamespace(interval=1440, scheduled="tomorrow", status="scheduled",
                             delete=lambda: deleted.append(future))
    state = SimpleNamespace(schedule_enabled=False, interval_minutes=1440)
    JobRunner.jobs = [future]
    service = SyncService(job_runner=JobRunner)
    assert service.reconcile_schedule(state) is None
    assert deleted == [future]
    assert JobRunner.calls == []


@pytest.mark.parametrize("status", ["pending", "running"])
def test_schedule_reconciliation_preserves_immediate_manual_job(status):
    state = SimpleNamespace(schedule_enabled=True, interval_minutes=60,
                            last_enqueue_error="", save=lambda **kwargs: None)
    manual = SimpleNamespace(instance=state, interval=None, status=status, deleted=False,
                             delete=lambda: setattr(manual, "deleted", True))
    SourceFaithfulJobRunner.jobs = [manual]
    result = SyncService(job_runner=SourceFaithfulJobRunner).reconcile_schedule(state)
    assert result.interval == 60
    assert result is not manual
    assert manual.deleted is False


def test_schedule_restores_runner_lookup_after_reconciliation_error():
    original = SourceFaithfulJobRunner.get_jobs
    state = SimpleNamespace(schedule_enabled=True, interval_minutes=60,
                            last_enqueue_error="", save=lambda **kwargs: None)

    def fail_once(cls, **kwargs):
        raise RuntimeError("queue unavailable")

    SourceFaithfulJobRunner.enqueue_once = classmethod(fail_once)
    try:
        assert SyncService(job_runner=SourceFaithfulJobRunner).reconcile_schedule(state) is None
        assert SourceFaithfulJobRunner.__dict__["get_jobs"].__func__ is original.__func__
    finally:
        SourceFaithfulJobRunner.enqueue_once = JobRunner.enqueue_once.__func__


def test_schedule_uses_singleton_and_interval(monkeypatch):
    state = SimpleNamespace(schedule_enabled=True, interval_minutes=1440,
                            save=lambda **kwargs: None)
    service = SyncService(job_runner=JobRunner)
    result = service.reconcile_schedule(state)
    assert result.interval == 1440
    assert JobRunner.calls[-1] == ("once", {
        "instance": state, "interval": 1440, "queue_name": "netbox_sync_plugin.sync",
        "job_timeout": 43200, "include_assetexplorer": True,
    })


def test_manual_deduplication_against_active_history():
    service = SyncService(job_runner=JobRunner)
    existing = object()
    service.has_active_run = lambda: existing
    assert service.enqueue(trigger="manual") is existing


def test_manual_enqueue_is_immediate_even_when_recurring_job_exists(monkeypatch):
    future = SimpleNamespace(interval=1440, scheduled="tomorrow", delete=lambda: None)
    JobRunner.jobs = [future]
    state = SimpleNamespace()
    monkeypatch.setattr("netbox_sync_plugin.services.SyncState.objects.get_or_create",
                        lambda key, defaults: (state, False))
    created = SimpleNamespace(job=None, save=lambda **kwargs: None)
    monkeypatch.setattr("netbox_sync_plugin.services.SyncRun.objects.create",
                        lambda **kwargs: created)
    service = SyncService(job_runner=JobRunner)
    monkeypatch.setattr(service, "has_active_run", lambda: None)
    run = service.enqueue(trigger="manual")
    assert run.job is not future
    assert "schedule_at" not in run.job.__dict__
    assert JobRunner.calls == [("immediate", {
        "instance": state, "user": None, "queue_name": "netbox_sync_plugin.sync",
        "job_timeout": 43200, "include_assetexplorer": True,
    })]


def test_worker_timeout_comes_from_plugin_configuration():
    assert SyncService().worker_timeout == 43200


def test_lock_timeout_waits_then_fails_closed(monkeypatch):
    service = SyncService()
    attempts = []
    lock = SimpleNamespace(acquire=lambda: attempts.append(1) or False)
    monkeypatch.setattr("netbox_sync_plugin.services.RedisExecutionLock", lambda client: lock)
    import sys
    monkeypatch.setitem(sys.modules, "django_rq", SimpleNamespace(get_connection=lambda queue: object()))
    clock = [0.0]
    monkeypatch.setattr("netbox_sync_plugin.services.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("netbox_sync_plugin.services.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(RedisLockError):
        service.acquire_lock(timeout=1)
    assert len(attempts) == 2


def test_execute_cli_forwards_lock_timeout(monkeypatch):
    service = SyncService()
    calls = []
    run = SimpleNamespace(finish=lambda status, summary: None)
    monkeypatch.setattr(service, "create_cli_run", lambda include: run)
    monkeypatch.setattr(service, "acquire_lock", lambda timeout: calls.append(timeout) or (_ for _ in ()).throw(
        RedisLockError("busy")))
    result = service.execute_cli(include_assetexplorer=False, lock_timeout=3)
    assert calls == [3]
    assert result.status == "failed"


def test_initial_state_uses_resolved_plugin_defaults(monkeypatch):
    from django.conf import settings
    captured = {}
    state = SimpleNamespace()
    monkeypatch.setattr(settings, "PLUGINS_CONFIG", {"netbox_sync_plugin": {
        "schedule_enabled": True,
        "interval_minutes": 60,
        "retention_runs": 5,
        "max_issues_per_run": 10,
        "issue_message_max_length": 120,
    }})
    monkeypatch.setattr("netbox_sync_plugin.services.SyncState.objects.get_or_create",
                        lambda **kwargs: captured.update(kwargs) or (state, True))
    assert SyncService.get_state() is state
    assert captured == {"key": "default", "defaults": {
        "schedule_enabled": True,
        "interval_minutes": 60,
        "retention_runs": 5,
        "max_issues_per_run": 10,
        "issue_message_max_length": 120,
    }}


def test_nonzero_exit_with_valid_result_preserves_partial_history(monkeypatch, tmp_path):
    import json
    from netbox_sync import worker
    class FailedProcess:
        returncode = 1
        stdout = iter(())
        def wait(self, timeout=None):
            return self.returncode
    def popen(command, **kwargs):
        with open(command[command.index("--result-file") + 1], "w") as stream:
            json.dump({"succeeded": False, "partial": True,
                       "assetexplorer_state": "failed", "counters": {"failed": 1},
                       "issues": [{"message": "safe failure"}]}, stream)
        return FailedProcess()
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    result = worker.run_subprocess(lock=type("Lock", (), {"release": lambda self: True})(),
                                   result_directory=tmp_path)
    assert result.status == "partial"
    assert result.snapshot["counters"]["failed"] == 1


def test_lock_conflict_does_not_start_subprocess():
    service = SyncService()
    called = []
    service.acquire_lock = lambda: (_ for _ in ()).throw(RedisLockError("busy"))
    service.run_subprocess = lambda **kwargs: called.append(kwargs)
    run = SimpleNamespace(pk=1, trigger="manual", status="queued",
                          finish=lambda status, summary: setattr(run, "status", status))
    result = service.execute(run)
    assert result.status == "failed"
    assert run.status == "failed"
    assert called == []


def test_execution_persists_failure_and_logs_sanitized_output():
    service = SyncService()
    job = SimpleNamespace(log_output="")
    job.log = lambda record: setattr(job, "log_output", job.log_output + record.getMessage())
    run = SimpleNamespace(pk=12, trigger="manual", assetexplorer_state="pending", job=job,
                          status="queued", started_at=None, save=lambda **kwargs: None)
    lock = object()
    service.acquire_lock = lambda: lock
    persisted = []
    service.persist_result = lambda run, result: persisted.append(result)
    seen = {}

    def execute(**kwargs):
        seen.update(kwargs)
        kwargs["log_callback"]("failed token=fake-log-secret")
        from netbox_sync.worker import WorkerResult
        return WorkerResult("failed", False, summary="safe failure")

    service.run_subprocess = execute
    result = service.execute(run)

    assert result.status == "failed"
    assert persisted == [result]
    assert run.status == "running"
    assert run.started_at is not None
    assert "token=[redacted]" in job.log_output
    assert "fake-log-secret" not in job.log_output
    assert "job" not in seen


def test_cli_execution_uses_same_lock_and_subprocess_and_cleans_history():
    service = SyncService()
    service.acquire_lock = lambda: object()
    captured = {}
    service.run_subprocess = lambda **kwargs: captured.update(kwargs) or __import__(
        "netbox_sync.worker", fromlist=["WorkerResult"]).WorkerResult("succeeded", True)
    run = SimpleNamespace(pk=15, job=None, assetexplorer_state="skipped",
                          save=lambda **kwargs: None)
    service.create_cli_run = lambda include_assetexplorer: run
    service.persist_result = lambda *args: None
    service.execute_cli(include_assetexplorer=False, lock_timeout=0)
    assert captured["include_assetexplorer"] is False


def test_schedule_error_is_bounded_and_sanitized():
    class BrokenRunner:
        get_jobs = classmethod(lambda cls, instance: JobQuerySet(()))

        @classmethod
        def enqueue_once(cls, **kwargs):
            raise RuntimeError("schedule failed token=fake-schedule-secret")

    state = SimpleNamespace(schedule_enabled=True, interval_minutes=1440,
                            last_enqueue_error="", save=lambda **kwargs: None)
    result = SyncService(job_runner=BrokenRunner).reconcile_schedule(state)
    assert result is None
    assert "fake-schedule-secret" not in state.last_enqueue_error
    assert "[redacted]" in state.last_enqueue_error
