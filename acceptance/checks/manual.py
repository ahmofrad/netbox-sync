import json
from django.contrib.auth import get_user_model
from netbox_sync_plugin.services import SyncService
from netbox_sync_plugin.models import SyncRun

admin = get_user_model().objects.get(username="acceptance-admin")
service = SyncService()
run = service.enqueue(trigger="manual", requester=admin)
duplicate = service.enqueue(trigger="manual", requester=admin)
print(json.dumps({"run_id": run.pk, "newly_enqueued": run.newly_enqueued,
                  "status": run.status, "job_id": run.job_id,
                  "duplicate_id": duplicate.pk,
                  "duplicate_newly_enqueued": duplicate.newly_enqueued,
                  "duplicate_rejected": duplicate.pk == run.pk and not duplicate.newly_enqueued}))
