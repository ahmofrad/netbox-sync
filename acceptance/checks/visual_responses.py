"""Produce nonsecret synthetic Operations responses for rendered acceptance."""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import Client
from django.utils import timezone

from dcim.models import Device
from netbox_sync_plugin.health import InventoryHealth
from netbox_sync_plugin.models import SyncRun, SyncState

OUTPUT = Path("/tmp/netbox-sync-visual-responses")
OUTPUT.mkdir(exist_ok=True)
OPERATIONS = "/plugins/netbox-sync/operations/"


def response(client, name, expected_banner):
    result = client.get(OPERATIONS)
    assert result.status_code == 200, result.status_code
    html = result.content.decode()
    assert "acceptance-only" not in html and "Token " not in html
    (OUTPUT / f"{name}.json").write_text(
        json.dumps({"html": html, "expected_banner": expected_banner}), encoding="utf-8")


credentials = json.loads(Path("/opt/netbox-sync-runtime/credentials.json").read_text(encoding="utf-8"))
client = Client()
assert client.login(username=credentials["username"], password=credentials["password"])
response(client, "populated", "Selected run")

run = SyncRun.objects.filter(stage="pipeline", status__in=("succeeded", "partial", "failed", "cancelled")).first()
assert run is not None
original_status = run.status
original_failed = run.failed
original_summary = run.summary
run.status = "failed"
run.failed = 1
run.summary = "Synthetic collector unavailable"
run.save(update_fields=("status", "failed", "summary"))
response(client, "failure", "Selected run Failed")

state = SyncState.objects.first()
heartbeat = state.last_heartbeat
state.last_heartbeat = timezone.now() - timedelta(hours=1)
state.save(update_fields=("last_heartbeat",))
response(client, "stale-heartbeat", "Worker heartbeat stale")

originals = {name: getattr(run, name) for name in ("status", "queued_at", "started_at", "finished_at")}
run.status = "queued"
run.started_at = None
run.finished_at = None
run.queued_at = timezone.now()
run.save(update_fields=("status", "started_at", "finished_at", "queued_at"))
response(client, "queued", "Synchronization queued")
run.status = "running"
run.started_at = timezone.now()
state.last_heartbeat = timezone.now()
state.save(update_fields=("last_heartbeat",))
run.save(update_fields=("status", "started_at"))
response(client, "running", "Synchronization running")

with patch("netbox_sync_plugin.views.SyncRun.objects.filter", return_value=SyncRun.objects.none()), \
        patch("netbox_sync_plugin.views.SyncState.objects.first", side_effect=RuntimeError("synthetic unavailable")):
    response(client, "empty-error", "No completed synchronization history")
with patch("netbox_sync_plugin.views.current_health", return_value=InventoryHealth(False, message="Inventory health unavailable")):
    response(client, "unavailable-health", "Inventory health unavailable")

run.status = original_status
run.failed = original_failed
run.summary = original_summary
run.save(update_fields=("status", "failed", "summary"))

for name, value in originals.items():
    setattr(run, name, value)
run.save(update_fields=tuple(originals))
state.last_heartbeat = heartbeat
state.save(update_fields=("last_heartbeat",))
print(json.dumps({"responses": sorted(path.stem for path in OUTPUT.glob("*.json")),
                  "output": str(OUTPUT), "credentials_in_artifacts": False}))
