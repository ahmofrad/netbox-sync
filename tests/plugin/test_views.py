"""Operations permission, CSRF, history selection, and presentation tests."""
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from netbox_sync_plugin import views


class User:
    is_authenticated = True

    def __init__(self, *permissions):
        self.permissions = set(permissions)

    def has_perm(self, permission):
        return permission in self.permissions


class CompletedRuns:
    def __init__(self, records):
        self.records = records

    def order_by(self, *args):
        return self

    def __getitem__(self, key):
        return self.records[key]


def setup_view(monkeypatch, user, records=(), health=None):
    request = RequestFactory().get("/plugins/netbox-sync/operations/")
    request.user = user
    view = views.OperationsView()
    view.request = request
    monkeypatch.setattr(views.SyncRun.objects, "filter",
                        lambda **kwargs: CompletedRuns(records))
    monkeypatch.setattr(views, "current_health", lambda request: health)
    return view, request


def test_operations_requires_private_history_permission(monkeypatch):
    view, request = setup_view(monkeypatch, User())
    assert view.get_required_permission() == "netbox_sync_plugin.view_syncrun"
    assert view.has_permission() is False
    view.request.user = User("netbox_sync_plugin.view_syncrun")
    assert view.has_permission() is True


def test_operations_selects_only_recent_completed_history(monkeypatch):
    counters = dict(created=0, updated=0, adopted=0, unchanged=0, discovered=0)
    first = SimpleNamespace(pk=1, status="succeeded", finished_at="yesterday", **counters)
    second = SimpleNamespace(pk=2, status="partial", finished_at="today", **counters)
    view, request = setup_view(monkeypatch, User("netbox_sync_plugin.view_syncrun"), [first, second])
    request.GET = request.GET.copy()
    request.GET["run"] = "2"
    context = view.get_context_data(request=request)
    assert context["selected_run"] is second
    assert context["runs"] == [first, second]
    assert context["health"] is None


def test_operations_distinguishes_queued_running_and_stale_heartbeat(monkeypatch):
    from datetime import timedelta
    from django.utils import timezone
    state = SimpleNamespace(schedule_enabled=True, interval_minutes=60,
                            last_heartbeat=timezone.now() - timedelta(hours=1))
    active = SimpleNamespace(status="queued")
    service = SimpleNamespace(has_active_run=lambda: active,
                              next_scheduled_run=lambda state: "next-run")
    monkeypatch.setattr(views, "SyncService", lambda: service)
    monkeypatch.setattr(views.SyncState.objects, "first", lambda: state)
    view, request = setup_view(monkeypatch, User("netbox_sync_plugin.view_syncrun"))
    context = view.get_context_data(request=request)
    assert context["worker_state"] == "queued"
    assert context["heartbeat_state"] == "stale"
    assert context["next_scheduled_run"] == "next-run"
    active.status = "running"
    state.last_heartbeat = timezone.now()
    context = view.get_context_data(request=request)
    assert context["worker_state"] == "running"
    assert context["heartbeat_state"] == "fresh"


def test_run_requires_independent_permission_and_post_only(monkeypatch):
    view = views.RunSyncView()
    view.request = RequestFactory().get("/plugins/netbox-sync/run/")
    view.request.user = User("netbox_sync_plugin.view_syncrun")
    assert view.has_permission() is False
    assert not hasattr(view, "get")
    view.request.user = User("netbox_sync_plugin.run_syncrun")
    assert view.has_permission() is True


@pytest.mark.parametrize("confirmation", [None, "", "no", "false", "0"])
def test_run_rejects_missing_or_false_confirmation(monkeypatch, confirmation):
    view = views.RunSyncView()
    data = {} if confirmation is None else {"confirm": confirmation}
    view.request = RequestFactory().post("/plugins/netbox-sync/run/", data)
    view.request.user = User("netbox_sync_plugin.run_syncrun")
    enqueued = []
    monkeypatch.setattr(views, "SyncService", lambda: SimpleNamespace(enqueue=lambda **kw: enqueued.append(kw)))
    response = view.post(view.request)
    assert response.status_code == 302
    assert enqueued == []


def test_run_accepts_explicit_confirmation(monkeypatch):
    view = views.RunSyncView()
    view.request = RequestFactory().post("/plugins/netbox-sync/run/", {"confirm": "yes"})
    view.request.user = User("netbox_sync_plugin.run_syncrun")
    enqueued = []
    monkeypatch.setattr(views, "SyncService", lambda: SimpleNamespace(enqueue=lambda **kw: enqueued.append(kw)))
    response = view.post(view.request)
    assert response.status_code == 302
    assert enqueued == [{"trigger": "manual", "requester": view.request.user}]


def test_run_reports_new_enqueue_and_duplicate_distinctly(monkeypatch):
    notifications = []
    monkeypatch.setattr(views, "_notify", lambda request, level, text: notifications.append(text))
    view = views.RunSyncView()
    view.request = RequestFactory().post("/plugins/netbox-sync/run/", {"confirm": "yes"})
    view.request.user = User("netbox_sync_plugin.run_syncrun")
    new_run = SimpleNamespace(pk=7, status="queued", newly_enqueued=True)
    monkeypatch.setattr(views, "SyncService", lambda: SimpleNamespace(enqueue=lambda **kw: new_run))
    view.post(view.request)
    assert notifications == ["Synchronization run 7 was queued."]
    new_run.newly_enqueued = False
    view.post(view.request)
    assert notifications[-1] == "Synchronization run 7 is already queued; no duplicate was created."
    new_run.status = "failed"
    view.post(view.request)
    assert notifications[-1] == "Synchronization could not be queued. Check worker and queue status."


def test_template_uses_local_assets_accessible_chart_and_confirmed_csrf_post():
    from pathlib import Path
    template = Path("netbox_sync_plugin/templates/netbox_sync_plugin/operations.html").read_text(encoding="utf-8")
    script = Path("netbox_sync_plugin/static/netbox_sync_plugin/operations.js").read_text(encoding="utf-8")
    stylesheet = Path("netbox_sync_plugin/static/netbox_sync_plugin/operations.css").read_text(encoding="utf-8")
    assert "{% csrf_token %}" in template
    assert 'name="confirm" value="yes"' in template
    assert "data-health-chart" in template and "role=\"img\"" in template
    assert "data-healthy" in template and "data-attention" in template and "data-offline" in template
    assert "operations.js" in template and "operations.css" in template
    assert "https://" not in template and "fetch(" not in script
    assert "createElementNS" in script
    assert "#dce4de" in stylesheet and "prefers-reduced-motion" in stylesheet


def test_navigation_exposes_operations_only_with_history_permission():
    from netbox_sync_plugin import navigation
    assert len(navigation.menu_items) == 1
    item = navigation.menu_items[0]
    assert item.link == "plugins:netbox_sync_plugin:operations"
    assert item.permissions == ("netbox_sync_plugin.view_syncrun",)


def test_current_health_requires_device_permission_and_restricts_queryset(monkeypatch):
    request = SimpleNamespace(user=User("dcim.view_device"))
    records = []
    manager = SimpleNamespace(restrict=lambda user, action: records.append((user, action)) or [1])
    monkeypatch.setitem(__import__("sys").modules, "dcim.models", SimpleNamespace(
        Device=SimpleNamespace(objects=manager)))
    monkeypatch.setattr(views.InventoryHealth, "from_queryset", lambda records: "restricted-health")
    assert views.current_health(request) == "restricted-health"
    assert records == [(request.user, "view")]
