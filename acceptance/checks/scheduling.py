"""Real NetBox/RQ recurrence and concurrent PostgreSQL scheduling checks."""
import json
import os
import sys
import time
from datetime import timedelta
from multiprocessing import Process, Queue

sys.path.insert(0, "/opt/netbox/netbox")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")

from django import setup
from django.db import close_old_connections
from django.utils import timezone

setup()

from core.choices import JobStatusChoices
from core.models import Job
from django.contrib.auth import get_user_model
from netbox_sync_plugin.jobs import SyncPipelineJob
from netbox_sync_plugin.models import SyncRun, SyncState
from netbox_sync_plugin.services import SyncService

ACTIVE = (JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_SCHEDULED,
          JobStatusChoices.STATUS_RUNNING)


def jobs(state):
    return list(SyncPipelineJob.get_jobs(state).order_by("pk").values(
        "pk", "status", "interval", "scheduled", "job_id"))


def wait_for(predicate, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for real RQ state")


def concurrent_worker(action, output):
    try:
        close_old_connections()
        service = SyncService()
        state = SyncState.objects.get(key="default")
        if action == "enqueue":
            result = service.enqueue(trigger="manual")
            output.put({"run": result.pk, "new": result.newly_enqueued})
        else:
            job = service.reconcile_schedule(state)
            output.put({"job": job.pk if job else None,
                        "error": state.last_enqueue_error})
    except Exception as exc:
        output.put({"exception": type(exc).__name__})
    finally:
        close_old_connections()


def main():
    service = SyncService()
    state = service.get_state()
    state.schedule_enabled = True
    state.interval_minutes = 1
    state.save()
    scheduled = service.reconcile_schedule(state)
    assert scheduled and scheduled.interval == 1
    initial_job_id = scheduled.pk
    reconciled = service.reconcile_schedule(state)
    assert reconciled and reconciled.interval == 1
    assert len([j for j in jobs(state) if j["interval"] and j["status"] in ACTIVE]) == 1
    assert len([j for j in jobs(state) if j["interval"] and j["status"] in ACTIVE]) == 1

    admin = get_user_model().objects.get(username="acceptance-admin")
    pending = service.enqueue(trigger="manual", requester=admin)
    assert pending.newly_enqueued
    pending_job_id = pending.job_id
    assert pending.job.interval is None
    reconciled = service.reconcile_schedule(state)
    assert reconciled and reconciled.interval == 1
    assert len([j for j in jobs(state) if j["interval"] and j["status"] in ACTIVE]) == 1
    assert Job.objects.get(pk=pending_job_id).status in ACTIVE
    assert service.has_active_run().pk == pending.pk
    wait_for(lambda: SyncRun.objects.get(pk=pending.pk).status == "succeeded")
    assert Job.objects.get(pk=pending_job_id).status == JobStatusChoices.STATUS_COMPLETED

    # Keep an immediate job in RQ's running state while reconciliation occurs.
    os.environ["NETBOX_SYNC_ACCEPTANCE_DELAY"] = "8"
    running = service.enqueue(trigger="manual", requester=admin)
    wait_for(lambda: SyncRun.objects.get(pk=running.pk).status == "running")
    running_job_id = running.job_id
    reconciled = service.reconcile_schedule(state)
    assert reconciled and reconciled.interval == 1
    assert len([j for j in jobs(state) if j["interval"] and j["status"] in ACTIVE]) == 1
    assert Job.objects.get(pk=running_job_id).status == JobStatusChoices.STATUS_RUNNING
    assert Job.objects.get(pk=running_job_id).interval is None
    wait_for(lambda: SyncRun.objects.get(pk=running.pk).status == "succeeded", timeout=30)

    scheduled_runs = wait_for(lambda: list(SyncRun.objects.filter(
        trigger="scheduled", status="succeeded").order_by("pk").values_list("pk", flat=True))
        if SyncRun.objects.filter(trigger="scheduled", status="succeeded").count() >= 2 else None)
    current = wait_for(lambda: jobs(state) if not [
        j for j in jobs(state) if not j["interval"] and j["status"] in ACTIVE] else None,
        timeout=30)
    recurring = [j for j in current if j["interval"] and j["status"] in ACTIVE]
    assert len(recurring) == 1

    # Exercise simultaneous callers against the real PostgreSQL advisory lock.
    close_old_connections()
    processes = []
    output = Queue()
    for action in ("enqueue", "schedule", "enqueue", "schedule"):
        process = Process(target=concurrent_worker, args=(action, output))
        process.start()
        processes.append(process)
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert not any("exception" in result for result in results), results
    assert sum(result.get("new", False) for result in results) <= 1
    assert len([j for j in jobs(state) if j["interval"] and j["status"] in ACTIVE]) == 1
    assert SyncRun.objects.filter(status__in=("queued", "running")).count() <= 1
    wait_for(lambda: not SyncRun.objects.filter(status__in=("queued", "running")).exists(), timeout=30)

    state.schedule_enabled = False
    state.save()
    assert service.reconcile_schedule(state) is None
    assert not [j for j in jobs(state) if j["interval"] and j["status"] in ACTIVE]
    before = SyncRun.objects.count()
    time.sleep(3)
    assert SyncRun.objects.count() == before
    print(json.dumps({
        "reconciliation_idempotent": True,
        "pending_manual_job_preserved": pending_job_id,
        "running_manual_job_preserved": running_job_id,
        "two_scheduled_cycles": scheduled_runs[:2],
        "concurrent_results": results,
        "concurrent_single_recurring_job": True,
        "disabled_without_backlog": True,
    }, default=str))


if __name__ == "__main__":
    main()
