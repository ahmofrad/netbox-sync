"""Credential, transport, and destructive-action security boundaries."""
import ast
from pathlib import Path

import pytest
from django.template import Context, Template
from django.test import Client

from netbox_sync_mcp import netbox_api
from netbox_sync_plugin.models import SyncRun


@pytest.mark.parametrize("path", ["/operations/", "/run/"])
def test_anonymous_operations_requests_redirect_without_mutation(path):
    response = Client().get(path)
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_view_only_operations_has_no_run_form_or_canary(monkeypatch):
    from netbox_sync_plugin import views
    monkeypatch.setattr(views.SyncRun.objects, "filter", lambda **kwargs: [])
    monkeypatch.setattr(views.SyncState.objects, "first", lambda: None)
    monkeypatch.setattr(views.SyncService, "has_active_run", lambda self: None)
    monkeypatch.setattr(views, "current_health", lambda request: None)
    user = type("User", (), {
        "is_authenticated": True,
        "has_perm": lambda self, permission: permission == "netbox_sync_plugin.view_syncrun",
    })()
    client = Client()
    from django.contrib.auth.middleware import AuthenticationMiddleware
    monkeypatch.setattr(AuthenticationMiddleware, "process_request",
                        lambda middleware, request: setattr(request, "user", user))
    response = client.get("/operations/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "name=\"confirm\"" not in content
    assert "fake-html-canary-secret" not in content


def test_template_escapes_sanitized_but_hostile_history_text():
    rendered = Template("{% autoescape on %}{{ value }}{% endautoescape %}").render(
        Context({"value": "<script>window.bad=true</script>"}))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_mcp_and_plugin_modules_have_no_execution_or_non_get_boundary():
    api_path = Path(netbox_api.__file__)
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    request_calls = [node for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and isinstance(node.func.value, ast.Name)
                     and node.func.value.id == "requests"]
    assert [call.func.attr for call in request_calls] == ["get"]
    server = Path("netbox_sync_mcp/server.py").read_text(encoding="utf-8")
    assert "netbox_sync_plugin" not in server
    assert "SyncService" not in server and "enqueue" not in server


def test_run_permission_matches_netbox_generated_permission_name():
    from netbox_sync_plugin.views import RunSyncView
    view = RunSyncView()
    assert view.get_required_permission() == "netbox_sync_plugin.run_syncrun"
    assert SyncRun._meta.permissions == [("run_syncrun", "Can enqueue synchronization")]


def test_plugin_has_no_inventory_destructive_view_or_delete_urls():
    from netbox_sync_plugin import views
    from netbox_sync_plugin.urls import urlpatterns
    public_actions = {name for name in dir(views.RunSyncView) if not name.startswith("_")}
    assert not ({"get", "put", "patch", "delete"} & public_actions)
    assert {route.name for route in urlpatterns} == {"operations", "run"}
    assert SyncRun._meta.model_name == "syncrun"
