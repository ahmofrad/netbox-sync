"""Real post-commit RQ failure and completed-run retention checks."""
import json
from unittest.mock import patch

import django_rq
from django.test import RequestFactory
from django.utils import timezone

from core.models import Job
from netbox_sync_plugin.models import SyncIssue, SyncRun
from netbox_sync_plugin.services import SyncService
from netbox_sync_plugin.views import RunSyncView


class RejectingQueue:
    def enqueue(self, *args, **kwargs):
        raise RuntimeError("Synthetic RQ outage token=fake-acceptance-canary")


def request_feedback(run):
    request = RequestFactory().post("/plugins/netbox-sync/run/", {"confirm": "yes"})
    request.user = run.requester
    request.session = {}
    setattr(request, "_messages", [])
    # Use the actual message storage API without depending on middleware.
    from django.contrib.messages.storage.fallback import FallbackStorage
    request._messages = FallbackStorage(request)
    response = RunSyncView.as_view()(request)
    return response.status_code, [str(message) for message in request._messages]


def main():
    service = SyncService()
    with patch.object(django_rq, "get_queue", lambda *args, **kwargs: RejectingQueue()):
        run = service.enqueue(trigger="manual")
    run.refresh_from_db()
    assert not run.newly_enqueued
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.job_id is not None
    assert "fake-acceptance-canary" not in run.summary
    assert "RuntimeError" in run.summary
    assert "Synchronization could not be queued" in run.summary
    job_status = run.job.status
    assert job_status == "pending"
    assert run.job.data is None

    # Confirm the failed history yields the same sanitized operator response.
    from django.contrib.auth import get_user_model
    run.requester = get_user_model().objects.get(username="acceptance-admin")
    with patch("netbox_sync_plugin.views.SyncService.enqueue", return_value=run):
        status, feedback = request_feedback(run)
    assert status == 302
    assert any("could not be queued" in message for message in feedback)
    assert not any("fake-acceptance-canary" in message for message in feedback)

    # Use isolated synthetic rows to prove completed-only retention and cascades.
    state = service.get_state()
    state.schedule_enabled = False
    state.save()
    old = SyncRun.objects.create(trigger="manual", status="failed",
                                 finished_at=timezone.now())
    SyncIssue.record(old, category="synthetic", message="retention fixture")
    active = SyncRun.objects.create(trigger="manual", status="running")
    latest = SyncRun.objects.create(trigger="manual", status="succeeded",
                                    finished_at=timezone.now())
    SyncRun.enforce_retention(1)
    assert not SyncRun.objects.filter(pk=old.pk).exists()
    assert SyncRun.objects.filter(pk=active.pk).exists()
    assert SyncRun.objects.filter(pk=latest.pk).exists()
    assert not SyncIssue.objects.filter(run_id=old.pk).exists()
    active.delete()
    latest.delete()
    print(json.dumps({
        "post_commit_run_failed": run.pk,
        "core_job_remained_unexecuted": job_status,
        "sanitized_summary": run.summary,
        "operator_feedback_sanitized": True,
        "completed_only_retention": True,
        "issue_cascade": True,
    }))


main()
