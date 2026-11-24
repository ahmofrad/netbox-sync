"""Run, schedule, or clean up managed synchronization from NetBox."""
from django.core.management.base import BaseCommand

from netbox_sync_plugin.models import SyncState
from netbox_sync_plugin.services import SyncService


class Command(BaseCommand):
    help = "Manage NetBox synchronization execution, scheduling, and history"

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        run = subparsers.add_parser("run", help="Run discovery then AssetExplorer")
        run.add_argument("--skip-assetexplorer", action="store_true")
        run.add_argument("--lock-timeout", type=int, default=0,
                         help="Seconds to wait for shared ownership (default: fail immediately)")
        subparsers.add_parser("schedule", help="Install or update the native recurring job")
        subparsers.add_parser("cleanup", help="Apply configured completed-run retention")

    def handle(self, *args, **options):
        service = SyncService()
        state = service.get_state()
        if options["action"] == "schedule":
            job = service.reconcile_schedule(state)
            if job is None:
                self.stdout.write("Synchronization scheduling is disabled or could not be scheduled")
                return
            self.stdout.write(self.style.SUCCESS(f"Synchronization scheduled (job {job.pk})"))
            return
        if options["action"] == "cleanup":
            service.cleanup(state.retention_runs)
            self.stdout.write(self.style.SUCCESS("Synchronization history cleanup completed"))
            return
        result = service.execute_cli(include_assetexplorer=not options["skip_assetexplorer"],
                                     lock_timeout=options["lock_timeout"])
        self.stdout.write(result.summary or f"Synchronization {result.status}")
        if not result.succeeded:
            raise SystemExit(1)
