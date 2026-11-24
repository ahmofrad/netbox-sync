"""Queue deduplication and fresh-process execution coordination."""
import os
import time
from contextlib import nullcontext

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from netbox_sync.locking import RedisExecutionLock, RedisLockError
from netbox_sync.report import sanitize_text
from netbox_sync.worker import WorkerResult, run_subprocess
from .constants import COUNTER_FIELDS, QUEUE_NAME
from .models import SyncFamilyResult, SyncIssue, SyncRun, SyncState


def _scheduling_lock():
    try:
        from django_pglocks import advisory_lock
        from netbox.constants import ADVISORY_LOCK_KEYS
    except ImportError:
        if getattr(settings, "NETBOX_SYNC_TESTING", False):
            return nullcontext()
        raise
    return advisory_lock(ADVISORY_LOCK_KEYS["job-schedules"])


class SyncService:
    def __init__(self, job_runner=None):
        self.job_runner = job_runner

    def _job_runner(self):
        if self.job_runner is None:
            from .jobs import SyncPipelineJob
            self.job_runner = SyncPipelineJob
        return self.job_runner

    def has_active_run(self):
        return SyncRun.objects.filter(stage="pipeline", status__in=("queued", "running")).first()

    def enqueue(self, *, trigger="manual", requester=None, include_assetexplorer=True):
        run = None
        try:
            # NetBox uses this advisory lock for recurrence reconciliation. Use
            # the same lock for immediate work so history deduplication and job
            # selection cannot race each other across web/worker processes.
            with _scheduling_lock():
                with transaction.atomic():
                    existing = self.has_active_run()
                    if existing:
                        try:
                            existing.newly_enqueued = False
                        except AttributeError:
                            pass
                        return existing
                    state = self.get_state()
                    run = SyncRun.objects.create(
                        trigger=trigger, requester=requester,
                        assetexplorer_state="pending" if include_assetexplorer else "skipped")
                    # enqueue_once() may return a future recurring job. Manual
                    # work always creates its own immediate RQ job/history row.
                    job = self._job_runner().enqueue(
                        instance=state, user=requester, queue_name=QUEUE_NAME,
                        job_timeout=self.worker_timeout,
                        include_assetexplorer=include_assetexplorer)
                    run.job = job
                    run.save(update_fields=("job",))
        except Exception as exc:
            if run is None:
                raise
            # NetBox's RQ insertion is an on_commit callback. Its exception
            # occurs after the history/job transaction has committed, so make
            # that durable queued row terminally failed instead of abandoning it.
            run.finish("failed", sanitize_text(
                f"Synchronization could not be queued: {type(exc).__name__}: {exc}"))
            run.newly_enqueued = False
            return run
        run.newly_enqueued = True
        return run

    def next_scheduled_run(self, state):
        if state is None:
            return None
        try:
            jobs = [job for job in self._job_runner().get_jobs(state)
                    if getattr(job, "interval", None)
                    and getattr(job, "status", None) in ("pending", "scheduled")
                    and getattr(job, "scheduled", None)]
            return min(jobs, key=lambda job: job.scheduled).scheduled if jobs else None
        except Exception:
            return None

    def reconcile_schedule(self, state):
        runner = self._job_runner()
        if not state.schedule_enabled:
            with _scheduling_lock():
                for job in runner.get_jobs(state):
                    if getattr(job, "interval", None) and job.status in ("pending", "scheduled"):
                        job.delete()
            return None
        try:
            with _scheduling_lock():
                with transaction.atomic():
                    # NetBox 4.5.1 enqueue_once() otherwise picks an immediate
                    # job attached to the same instance and may delete it when
                    # its interval differs. Hide only immediate jobs while the
                    # native method reconciles the recurring schedule.
                    original_get_jobs = runner.get_jobs

                    def recurring_jobs(instance=None):
                        return original_get_jobs(instance).filter(interval__isnull=False)

                    runner.get_jobs = recurring_jobs
                    try:
                        job = runner.enqueue_once(
                            instance=state, interval=state.interval_minutes,
                            queue_name=QUEUE_NAME, job_timeout=self.worker_timeout,
                            include_assetexplorer=True)
                    finally:
                        runner.get_jobs = original_get_jobs
        except Exception as exc:
            state.last_enqueue_error = sanitize_text(f"Scheduling failed: {type(exc).__name__}: {exc}")
            state.save(update_fields=("last_enqueue_error",))
            return None
        state.last_enqueue_error = ""
        state.save(update_fields=("last_enqueue_error",))
        return job

    @staticmethod
    def plugin_setting(name):
        try:
            from netbox.plugins import get_plugin_config
            return get_plugin_config("netbox_sync_plugin", name)
        except (ImportError, ImproperlyConfigured):
            from . import NetBoxSyncConfig
            return settings.PLUGINS_CONFIG.get("netbox_sync_plugin", {}).get(
                name, NetBoxSyncConfig.default_settings[name])

    @classmethod
    def get_state(cls):
        defaults = {name: cls.plugin_setting(name) for name in
                    ("schedule_enabled", "interval_minutes", "retention_runs",
                     "max_issues_per_run", "issue_message_max_length")}
        state, created = SyncState.objects.get_or_create(key="default", defaults=defaults)
        return state

    @property
    def worker_timeout(self):
        configured = self.plugin_setting("worker_timeout")
        return int(getattr(settings, "NETBOX_SYNC_WORKER_TIMEOUT", configured))

    def acquire_lock(self, timeout=0):
        from django_rq import get_connection
        lock = RedisExecutionLock(get_connection(QUEUE_NAME))
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if lock.acquire():
                return lock
            if time.monotonic() >= deadline:
                raise RedisLockError("Another synchronization run owns the execution lock")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def run_subprocess(self, **kwargs):
        return run_subprocess(**kwargs)

    def execute(self, run, *, lock_timeout=0):
        try:
            lock = self.acquire_lock(lock_timeout) if lock_timeout else self.acquire_lock()
        except RedisLockError as exc:
            summary = sanitize_text(str(exc))
            run.finish("failed", summary)
            return WorkerResult("failed", False, summary=summary)
        run.status = "running"
        run.started_at = timezone.now()
        run.save(update_fields=("status", "started_at"))
        result = self.run_subprocess(lock=lock, run_id=run.pk,
                                     log_callback=self._job_logger(run.job),
                                     heartbeat_callback=self.heartbeat,
                                     timeout=self.worker_timeout,
                                     include_assetexplorer=run.assetexplorer_state != "skipped")
        self.persist_result(run, result)
        return result

    @staticmethod
    def heartbeat():
        SyncState.objects.update(last_heartbeat=timezone.now())

    def _job_logger(self, job):
        def write(message):
            clean = sanitize_text(message)
            if job is not None:
                import logging
                job.log(logging.LogRecord("netbox_sync", logging.INFO, "", 0,
                                         clean, (), None))
        return write

    def create_cli_run(self, include_assetexplorer=True):
        return SyncRun.objects.create(
            trigger="cli", assetexplorer_state="pending" if include_assetexplorer else "skipped")

    def execute_cli(self, *, include_assetexplorer=True, lock_timeout=0):
        run = self.create_cli_run(include_assetexplorer)
        return self.execute(run, lock_timeout=lock_timeout)

    def cleanup(self, limit):
        SyncRun.enforce_retention(limit)
        return limit

    def persist_result(self, run, result):
        from . import NetBoxSyncConfig
        snapshot = result.snapshot
        run.plugin_version = NetBoxSyncConfig.version
        run.source_revision = os.getenv("NETBOX_SYNC_SOURCE_REVISION", "")[:80]
        run.assetexplorer_state = snapshot.get("assetexplorer_state", run.assetexplorer_state)
        for key in COUNTER_FIELDS:
            setattr(run, key, int(snapshot.get("counters", {}).get(key, 0)))
        run.finish(result.status, result.summary)
        for key, family in snapshot.get("families", {}).items():
            values = {counter: family.get("counters", {}).get(counter, 0) for counter in COUNTER_FIELDS}
            SyncFamilyResult.objects.update_or_create(
                run=run, family_key=key,
                defaults={"display_label": family.get("display_label", key),
                          "status": family.get("status", "succeeded"), **values})
        state = SyncState.objects.first()
        for issue in snapshot.get("issues", []):
            family = run.families.filter(family_key=issue.get("family")).first()
            SyncIssue.record(run, family=family, category=issue.get("category", "no data"),
                             severity=issue.get("severity", "error"), source=issue.get("source", ""),
                             message=issue.get("message", "Synchronization issue"),
                             occurrences=issue.get("occurrences", 1))
        if state is not None:
            self.cleanup(state.retention_runs)
