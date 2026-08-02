"""Single-run entry logic: validate config, guard against overlapping
instances with a lockfile, run one full sync, return a cron-friendly
exit code. Scheduling is external (cron / systemd timer / Task Scheduler)."""
import atexit
import os
import time
import traceback

from netbox_sync.config import _validate_config, log
from netbox_sync.sync import run_sync

LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "netbox-sync.lock")
LOCK_MAX_AGE_SECONDS = 24 * 3600


def _acquire_lock(path=LOCK_FILE):
    """Create the lockfile exclusively; returns True when held. An existing
    lock older than LOCK_MAX_AGE_SECONDS is considered stale (crash) and
    replaced. The lock is released automatically at process exit."""
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age > LOCK_MAX_AGE_SECONDS:
            log("WARN", f"Stale lock file (>{int(age / 3600)}h old) — removing {path}")
            try: os.remove(path)
            except OSError: pass
        else:
            return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    def _release():
        try: os.remove(path)
        except OSError: pass
    atexit.register(_release)
    return True


def main():
    try:
        _validate_config()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1
    if not _acquire_lock():
        log("ERROR", f"Another instance is running (lock: {LOCK_FILE}) — exiting.")
        return 1
    try:
        run_sync()
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
        return 130
    except Exception:
        traceback.print_exc()
        return 1
    return 0
