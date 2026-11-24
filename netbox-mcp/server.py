"""Compatibility launcher; use ``netbox-sync-mcp`` for new installations."""
from netbox_sync_mcp.server import *  # noqa: F401,F403
from netbox_sync_mcp.server import main


if __name__ == "__main__":
    main()
