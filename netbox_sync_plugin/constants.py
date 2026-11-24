"""Nonsecret operational constants."""
QUEUE_NAME = "netbox_sync_plugin.sync"
STAGE_CHOICES = (("pipeline", "Pipeline"), ("discovery", "Discovery"), ("assetexplorer", "AssetExplorer"))
TRIGGER_CHOICES = (("scheduled", "Scheduled"), ("manual", "Manual"), ("cli", "CLI"))
STATUS_CHOICES = tuple((key, key.replace("_", " ").title()) for key in
                       ("queued", "running", "succeeded", "partial", "failed", "cancelled"))
COUNTER_FIELDS = ("discovered", "created", "updated", "adopted", "unchanged", "failed", "offline")
