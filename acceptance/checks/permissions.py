"""Live NetBox permission, CSRF, confirmation, and restricted-health checks."""
import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from dcim.models import Device
from netbox_sync_plugin.health import InventoryHealth
from netbox_sync_plugin.views import current_health
from netbox_sync_plugin.models import SyncRun
from users.models import ObjectPermission

User = get_user_model()
OPERATIONS = "/plugins/netbox-sync/operations/"
RUN = "/plugins/netbox-sync/run/"


def user(name):
    record, _ = User.objects.get_or_create(username=name, defaults={"is_active": True})
    record.set_password("acceptance-only-user-password")
    record.is_active = True
    record.save()
    return record


def permission(name, model, action, actions=None, constraints=None, users=()):
    record, _ = ObjectPermission.objects.get_or_create(
        name=name,
        defaults={"actions": actions or [action], "constraints": constraints, "enabled": True},
    )
    record.actions = actions or [action]
    record.constraints = constraints
    record.enabled = True
    record.save()
    record.object_types.set([ContentType.objects.get_for_model(model)])
    record.users.set(users)
    return record


def main():
    viewer = user("acceptance-viewer")
    runner = user("acceptance-runner")
    restricted = user("acceptance-restricted")
    permission("Acceptance Operations view", SyncRun, "view", users=[viewer, restricted])
    permission("Acceptance Operations run", SyncRun, "run", actions=["run"], users=[runner])
    first = Device.objects.get(name="fixture-server-1")
    permission("Acceptance all device reads", Device, "view", users=[viewer])
    permission("Acceptance one device read", Device, "view",
               constraints={"name": first.name}, users=[restricted])

    anonymous = Client()
    response = anonymous.get(OPERATIONS)
    assert response.status_code == 302 and "/login/" in response.url

    view_client = Client()
    assert view_client.login(username=viewer.username, password="acceptance-only-user-password")
    response = view_client.get(OPERATIONS)
    assert response.status_code == 200
    content = response.content.decode()
    assert 'name="confirm"' not in content
    assert view_client.get(RUN).status_code == 403
    assert view_client.post(RUN, {"confirm": "yes"}).status_code == 403

    run_client = Client()
    csrf_client = Client(enforce_csrf_checks=True)
    assert run_client.login(username=runner.username, password="acceptance-only-user-password")
    assert csrf_client.login(username=runner.username, password="acceptance-only-user-password")
    assert run_client.get(OPERATIONS).status_code == 403
    assert run_client.get(RUN).status_code == 405
    active_before = SyncRun.objects.filter(status__in=("queued", "running")).count()
    response = csrf_client.post(RUN, {"confirm": "yes"})
    assert response.status_code == 403
    assert SyncRun.objects.filter(status__in=("queued", "running")).count() == active_before
    response = run_client.post(RUN, {})
    assert response.status_code == 302
    assert SyncRun.objects.filter(status__in=("queued", "running")).count() == active_before

    restricted_client = Client()
    assert restricted_client.login(username=restricted.username,
                                   password="acceptance-only-user-password")
    response = restricted_client.get(OPERATIONS)
    assert response.status_code == 200
    health = current_health(response.wsgi_request)
    assert health is not None and health.total == 1
    allowed_ids = set(Device.objects.restrict(restricted, "view").values_list("pk", flat=True))
    assert allowed_ids == {first.pk}
    assert all(device_id in allowed_ids for check in health.checks.values()
               for device_id in check.ids)
    page = response.content.decode()
    assert all(f"/dcim/devices/{device_id}/" not in page
               for device_id in Device.objects.exclude(pk=first.pk).values_list("pk", flat=True))
    no_device_health = InventoryHealth.from_queryset(Device.objects.restrict(runner, "view"))
    assert no_device_health.total == 0

    print(json.dumps({
        "anonymous_redirected": True,
        "view_only_cannot_run": True,
        "run_only_cannot_view_history": True,
        "get_run_rejected": 405,
        "csrf_rejected_without_mutation": True,
        "missing_confirmation_without_mutation": True,
        "restricted_health_total": health.total,
        "restricted_device_ids": sorted(allowed_ids),
        "health_links_bounded_to_allowed_ids": True,
    }))


main()
