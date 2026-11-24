"""Live Redis conflicts, CLI fail-closed behavior, and process-tree cleanup."""
import json
import os
import subprocess
import threading
import time

import django_rq
from django.core.management import call_command

from netbox_sync.locking import RedisExecutionLock, RedisLockError
from netbox_sync.worker import run_subprocess
from netbox_sync_plugin.constants import QUEUE_NAME
from netbox_sync_plugin.models import SyncRun
from netbox_sync_plugin.services import SyncService


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main():
    for path in ("/tmp/netbox-sync-acceptance-child.marker",
                 "/tmp/netbox-sync-acceptance-grandchild.marker",
                 "/tmp/netbox-sync-acceptance-main.marker",
                 "/tmp/netbox-sync-acceptance-assetexplorer.marker"):
        try:
            os.remove(path)
        except OSError:
            pass
    client = django_rq.get_connection(QUEUE_NAME)
    foreign = RedisExecutionLock(client)
    assert foreign.acquire()
    before = SyncRun.objects.count()
    try:
        command = subprocess.run([
            "/opt/netbox/venv/bin/python", "/opt/netbox/netbox/manage.py",
            "netbox_sync", "run", "--skip-assetexplorer",
        ], capture_output=True, text=True, timeout=30)
        assert command.returncode == 1, command.stdout
        assert "owns the execution lock" in command.stdout
        assert SyncRun.objects.count() == before + 1
        conflict = SyncRun.objects.latest("pk")
        assert conflict.status == "failed" and conflict.started_at is None
        assert not os.path.exists("/tmp/netbox-sync-acceptance-child.marker")

        os.environ["SYNC_REDIS_URL"] = "redis://redis:6379/0"
        from netbox_sync.locking import configured_execution_lock
        try:
            with configured_execution_lock():
                raise AssertionError("Legacy Redis workflow acquired a foreign-owned lock")
        except RedisLockError:
            pass
        assert not os.path.exists("/tmp/netbox-sync-acceptance-child.marker")
    finally:
        foreign.release()

    # A real child and same-process-group grandchild must stop on ownership loss.
    os.environ["NETBOX_SYNC_ACCEPTANCE_TREE"] = "1"
    os.environ["NETBOX_SYNC_ACCEPTANCE_DELAY"] = "120"
    lock = RedisExecutionLock(client, heartbeat_interval=0.2)
    assert lock.acquire()
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault(
        "result", run_subprocess(lock=lock, run_id=998, timeout=30,
                                 heartbeat_interval=0.2)))
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if os.path.exists("/tmp/netbox-sync-acceptance-grandchild.marker"):
            break
        time.sleep(0.05)
    with open("/tmp/netbox-sync-acceptance-child.marker", encoding="utf-8") as marker:
        child_pid = int(marker.read())
    with open("/tmp/netbox-sync-acceptance-grandchild.marker", encoding="utf-8") as marker:
        grandchild_pid = int(marker.read())
    assert child_pid != os.getpid() and grandchild_pid != child_pid
    # Simulate ownership replacement without deleting a lock we still own.
    replaced = client.set(lock.key, "synthetic-other-owner", xx=True)
    assert replaced
    thread.join(timeout=15)
    assert not thread.is_alive()
    result = outcome["result"]
    assert result.status == "failed" and not result.succeeded
    assert "ownership" in result.summary
    time.sleep(0.5)
    assert not process_exists(child_pid)
    assert not process_exists(grandchild_pid)
    for path in ("/tmp/netbox-sync-acceptance-child.marker",
                 "/tmp/netbox-sync-acceptance-grandchild.marker",
                 "/tmp/netbox-sync-acceptance-main.marker",
                 "/tmp/netbox-sync-acceptance-assetexplorer.marker"):
        try:
            os.remove(path)
        except OSError:
            pass
    os.environ.pop("NETBOX_SYNC_ACCEPTANCE_TREE")
    os.environ["NETBOX_SYNC_ACCEPTANCE_DELAY"] = "2"
    remaining_owner = client.get(lock.key)
    assert remaining_owner is not None and remaining_owner.decode() == "synthetic-other-owner", remaining_owner
    # Remove only the explicitly injected replacement after proving that the
    # former owner did not delete it during cleanup.
    assert client.delete(lock.key) == 1
    print(json.dumps({
        "cli_conflict_run": conflict.pk,
        "cli_started_no_subprocess": True,
        "legacy_redis_conflict_failed_closed": True,
        "ownership_loss_result": result.summary,
        "child_terminated": child_pid,
        "grandchild_terminated": grandchild_pid,
        "foreign_lock_preserved_then_fixture_removed": True,
    }))


main()
