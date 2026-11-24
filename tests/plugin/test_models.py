"""Plugin-owned history model invariants."""
import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from netbox_sync_plugin.models import SyncFamilyResult, SyncIssue, SyncRun, SyncState


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


def test_models_declare_netbox_job_integration_without_runtime_injection():
    from django.contrib.contenttypes.fields import GenericRelation
    from django.db.models import ForeignKey

    assert isinstance(SyncState._meta.get_field("jobs"), GenericRelation)
    assert isinstance(SyncRun._meta.get_field("job"), ForeignKey)
    assert SyncRun._meta.get_field("job").remote_field.on_delete.__name__ == "SET_NULL"
    assert SyncRun._meta.get_field("job").remote_field.model._meta.label_lower == "core.job"


def test_state_is_singleton_and_has_nonsecret_operational_defaults():
    SyncState.objects.create(key="default")
    with pytest.raises(IntegrityError), transaction.atomic():
        SyncState.objects.create(key="default")
    state = SyncState.objects.get()
    assert state.interval_minutes == 1440
    assert state.retention_runs == 90
    assert state.max_issues_per_run == 500
    assert state.issue_message_max_length == 500


def test_run_family_issue_fields_and_permissions():
    run = SyncRun.objects.create(trigger="manual", stage="pipeline")
    family = SyncFamilyResult.objects.create(run=run, family_key="servers", display_label="Servers")
    issue = SyncIssue.record(run, family=family, category="no data", source="192.0.2.1",
                             message="request failed token=fake-canary-token")
    SyncIssue.record(run, family=family, category="no data", source="192.0.2.1",
                     message="request failed token=fake-canary-token")
    issue.refresh_from_db()
    assert issue.occurrences == 2
    assert "fake-canary-token" not in issue.message
    assert family.run == run
    assert "run_syncrun" in dict(SyncRun._meta.permissions)
    fields = {field.name for model in (SyncState, SyncRun, SyncFamilyResult, SyncIssue)
              for field in model._meta.fields}
    assert not any(word in field for field in fields for word in ("token", "password", "secret", "credential"))


def test_retention_keeps_newest_completed_runs_only():
    active = SyncRun.objects.create(status="running")
    for _ in range(4):
        SyncRun.objects.create(status="succeeded")
    SyncRun.enforce_retention(3)
    assert SyncRun.objects.filter(status="succeeded").count() == 3
    assert SyncRun.objects.filter(pk=active.pk).exists()
