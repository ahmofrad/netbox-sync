"""Explicit shared-lock wrapper for the existing ordered daily shell pipeline."""
import subprocess
import sys
from pathlib import Path

from netbox_sync.locking import RedisLockError, configured_execution_lock


def main():
    try:
        with configured_execution_lock():
            environment = dict(__import__("os").environ)
            environment["SYNC_LOCK_BACKEND"] = "file"
            completed = subprocess.run(
                ["bash", str(Path(__file__).with_name("run_daily_sync.sh"))],
                env=environment,
            )
            return completed.returncode
    except RedisLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
