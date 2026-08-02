#!/usr/bin/env python3
"""
sync_all_to_netbox.py — single-run entry point.

Scans IP ranges for iLO/Redfish BMCs (servers), HPE MSA storage arrays,
HPE B-Series (Brocade OEM) SAN switches, and Cisco Catalyst switches;
auto-creates/updates devices, interfaces, VLANs, cables and inventory in
NetBox; and marks unreachable devices offline.

Runs ONE full sync and exits (0 = success, 1 = error, 130 = Ctrl+C).
Schedule it externally — cron, systemd timer, or Task Scheduler (see README).
"""
import sys

from netbox_sync.main import main

if __name__ == "__main__":
    sys.exit(main())
