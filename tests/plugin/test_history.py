"""Durable run/family/issue history and retention integration tests."""
import pytest
from django.db import connection

from netbox_sync.worker import WorkerResult
from netbox_sync_plugin.models import SyncFamilyResult, SyncIssue, SyncRun, SyncState
from netbox_sync_plugin.services import SyncService


@pytest.fixture(autouse=True)
def plugin_tables():
    from django.contrib.auth import get_user_model
    from core.models import Job
    with connection.schema_editor() as editor:
        editor.create_model(get_user_model())
        editor.create_model(Job)
        for model in (SyncState, SyncRun, SyncFamilyResult, SyncIssue):
            editor.create_model(model)
    yield
    with connection.schema_editor() as editor:
        for model in (SyncIssue, SyncFamilyResult, SyncRun, SyncState, Job, get_user_model()):
            editor.delete_model(model)


def snapshot(summary="completed"):
    return {
        "summary": summary,
        "assetexplorer_state": "succeeded",
        "counters": {"discovered": 2, "created": 1, "updated": 1, "failed": 1},
        "families": {"servers": {"display_label": "Servers", "status": "partial",
                                  "counters": {"discovered": 2, "created": 1, "updated": 1,
                                               "failed": 1}}},
        "issues": [{"family": "servers", "source": "192.0.2.1", "category": "no data",
                    "severity": "error", "message": "collection failed", "occurrences": 1}],
    }


def test_post_commit_enqueue_failure_marks_committed_history_failed():
    from django.db import transaction
    from core.models import Job

    class BrokenQueueRunner:
        @classmethod
        def enqueue(cls, **kwargs):
            job = Job.objects.create(name="NetBox synchronization")
            transaction.on_commit(lambda: (_ for _ in ()).throw(RuntimeError(
                "queue unavailable token=fake-rq-canary")))
            return job

    run = SyncService(job_runner=BrokenQueueRunner).enqueue()
    run.refresh_from_db()
    assert run.newly_enqueued is False
    assert run.created == 0
    assert run.status == "failed"
    assert "fake-rq-canary" not in run.summary


def test_partial_history_persists_family_issue_and_job_link():
    from core.models import Job
    SyncState.objects.create(key="default", max_issues_per_run=2)
    job = Job.objects.create(name="sync")
    run = SyncRun.objects.create(trigger="scheduled", job=job)
    SyncService().persist_result(run, WorkerResult("partial", False, True,
                                                    summary="AssetExplorer failed",
                                                    snapshot=snapshot()))
    run.refresh_from_db()
    assert run.status == "partial" and run.finished_at is not None
    assert run.job == job
    assert run.discovered == 2 and run.failed == 1
    assert run.families.get().display_label == "Servers"
    assert run.issues.get().family == run.families.get()
    job.delete()
    run.refresh_from_db()
    assert run.job is None


def test_canonical_issue_is_associated_with_persisted_family():
    run = SyncRun.objects.create(trigger="scheduled")
    data = snapshot()
    data["issues"] = [{**data["issues"][0], "family": "servers"}]
    SyncService().persist_result(run, WorkerResult("partial", False, True, snapshot=data))
    assert run.issues.get().family.family_key == "servers"


def test_issue_cap_still_aggregates_known_issue_without_new_groups():
    SyncState.objects.create(key="default", max_issues_per_run=1)
    run = SyncRun.objects.create(trigger="manual")
    result = WorkerResult("failed", False, snapshot={
        **snapshot(), "issues": [
            {"family": "servers", "source": "same", "category": "no data", "message": "same failure"},
            {"family": "servers", "source": "different", "category": "no data", "message": "new failure"},
            {"family": "servers", "source": "same", "category": "no data", "message": "same failure"},
        ],
    })
    SyncService().persist_result(run, result)
    assert run.issues.count() == 1
    issue = run.issues.get()
    assert issue.message == "same failure"
    assert issue.occurrences == 2


def test_history_records_plugin_version_and_explicitly_configured_revision(monkeypatch):
    monkeypatch.setenv("NETBOX_SYNC_SOURCE_REVISION", "reviewed-build-123")
    run = SyncRun.objects.create(trigger="scheduled")
    SyncService().persist_result(run, WorkerResult("succeeded", True, snapshot=snapshot()))
    run.refresh_from_db()
    assert run.plugin_version == "1.0.0"
    assert run.source_revision == "reviewed-build-123"


def test_history_leaves_unconfigured_revision_empty(monkeypatch):
    monkeypatch.delenv("NETBOX_SYNC_SOURCE_REVISION", raising=False)
    run = SyncRun.objects.create(trigger="scheduled")
    SyncService().persist_result(run, WorkerResult("succeeded", True, snapshot=snapshot()))
    run.refresh_from_db()
    assert run.source_revision == ""


def test_completion_automatically_enforces_configured_retention():
    state = SyncState.objects.create(key="default", retention_runs=1)
    old = SyncRun.objects.create(status="failed")
    new = SyncRun.objects.create(trigger="scheduled")
    SyncService().persist_result(new, WorkerResult("succeeded", True, snapshot=snapshot()))
    assert not SyncRun.objects.filter(pk=old.pk).exists()
    assert SyncRun.objects.filter(pk=new.pk).exists()


def test_heartbeat_is_forwarded_during_active_execution():
    service = SyncService()
    state = SyncState.objects.create(key="default")
    run = SyncRun.objects.create(trigger="scheduled")
    seen = []
    service.acquire_lock = lambda: object()

    def execute(**kwargs):
        kwargs["heartbeat_callback"]()
        state.refresh_from_db()
        seen.append(state.last_heartbeat)
        return WorkerResult("succeeded", True, snapshot=snapshot())

    service.run_subprocess = execute
    service.execute(run)
    assert seen[0] is not None


def test_retention_cascades_only_stale_completed_history():
    old = SyncRun.objects.create(status="failed")
    new = SyncRun.objects.create(status="succeeded")
    active = SyncRun.objects.create(status="running")
    SyncFamilyResult.objects.create(run=old, family_key="old", display_label="Old")
    SyncIssue.record(old, category="failure", message="safe")
    SyncRun.enforce_retention(1)
    assert not SyncRun.objects.filter(pk=old.pk).exists()
    assert SyncRun.objects.filter(pk__in=(new.pk, active.pk)).count() == 2
    assert not SyncIssue.objects.filter(run_id=old.pk).exists()
