"""NetBox navigation exposes only the permission-protected Operations page."""
try:
    from netbox.plugins import PluginMenuItem

    menu_items = (
        PluginMenuItem(
            link="plugins:netbox_sync_plugin:operations",
            link_text="Netbox-sync Operations",
            permissions=("netbox_sync_plugin.view_syncrun",),
        ),
    )
except ImportError:
    class PluginMenuItem:
        """Metadata-only fallback for isolated tests without NetBox."""
        def __init__(self, link, link_text, permissions=()):
            self.link = link
            self.link_text = link_text
            self.permissions = permissions

    menu_items = (
        PluginMenuItem(
            link="plugins:netbox_sync_plugin:operations",
            link_text="Netbox-sync Operations",
            permissions=("netbox_sync_plugin.view_syncrun",),
        ),
    )
