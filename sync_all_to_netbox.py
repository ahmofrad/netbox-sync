#!/usr/bin/env python3
"""
sync_all_to_netbox.py — entry point.

Scans IP ranges for iLO/Redfish BMCs (servers), HPE MSA storage arrays,
and HPE B-Series (Brocade OEM) SAN switches; auto-creates/updates devices,
interfaces and inventory in NetBox; and marks unreachable devices offline.
Runs at 00:00 and 12:00 daily.

The implementation lives in the netbox_sync package; this script is just
the scheduler/entry point so existing invocations keep working unchanged.
"""
import time

import schedule

from netbox_sync.config import _validate_config, log
from netbox_sync.sync import run_sync

if __name__ == "__main__":
    try:
        _validate_config()
        schedule.every().day.at("00:00").do(run_sync)
        schedule.every().day.at("12:00").do(run_sync)
        log("INFO", "Scheduler started — runs at 00:00 and 12:00 daily.")
        log("INFO", "Running initial unified sync now ...")
        run_sync()
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
