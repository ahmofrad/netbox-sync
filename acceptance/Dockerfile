FROM netboxcommunity/netbox:v4.5.1
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip && rm -rf /var/lib/apt/lists/*
COPY dist/netbox_sync_plugin-1.0.0-py3-none-any.whl /tmp/netbox_sync_plugin-1.0.0-py3-none-any.whl
RUN SITE_PACKAGES=$(/opt/netbox/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') && /usr/bin/python3 -m pip install --target "$SITE_PACKAGES" /tmp/netbox_sync_plugin-1.0.0-py3-none-any.whl && /usr/bin/python3 -m pip install --upgrade --target "$SITE_PACKAGES" 'typing-extensions>=4.15' && /opt/netbox/venv/bin/python -c 'from mcp import ClientSession; import netbox_sync_mcp.server'
RUN printf '%s\n' '#!/bin/sh' 'exec /opt/netbox/venv/bin/python -c "from netbox_sync.pipeline import main; raise SystemExit(main())" "$@"' > /opt/netbox/venv/bin/netbox-sync && printf '%s\n' '#!/bin/sh' 'exec /opt/netbox/venv/bin/python -m netbox_sync_mcp.server "$@"' > /opt/netbox/venv/bin/netbox-sync-mcp && chmod 755 /opt/netbox/venv/bin/netbox-sync /opt/netbox/venv/bin/netbox-sync-mcp
USER netbox:root
