"""NetBox plugin configuration; safe to import outside NetBox for testing."""
try:
    from netbox.plugins import PluginConfig
except ImportError:
    from django.apps import AppConfig as PluginConfig


class NetBoxSyncConfig(PluginConfig):
    name = "netbox_sync_plugin"
    verbose_name = "Netbox-sync plugin"
    description = "Managed synchronization, history, and operations health"
    version = "1.0.0"
    base_url = "netbox-sync"
    min_version = "4.5.1"
    max_version = "4.5.1"
    queues = ["sync"]
    default_settings = {
        "schedule_enabled": False,
        "interval_minutes": 1440,
        "retention_runs": 90,
        "max_issues_per_run": 500,
        "issue_message_max_length": 500,
        "worker_timeout": 86400,
    }

    def ready(self):
        super().ready()
        from . import jobs  # noqa: F401
        from django.conf import settings
        testing = getattr(settings, "NETBOX_SYNC_TESTING", False)
        # Recurring work is anchored through SyncService.reconcile_schedule().
        # Never register an instance-less system job: rqworker would enqueue it
        # independently of plugin configuration and durable SyncState history.
        if not testing:
            from django.db import OperationalError, ProgrammingError
            from .models import SyncState
            try:
                from .services import SyncService
                SyncService.get_state()
            except (OperationalError, ProgrammingError):
                # Apps can load before migrations; scheduling also initializes.
                pass


config = NetBoxSyncConfig
