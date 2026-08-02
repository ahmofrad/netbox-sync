"""Shared pytest setup.

Makes the repo root importable and provides dummy credentials so that
``import netbox_sync`` succeeds without a real .env file (config validates
credentials only when the entry point runs). ``setdefault`` never overrides
a real environment.
"""
import os
import sys

import dotenv

# Tests must be hermetic: a real .env in the repo (e.g. production config on
# a dev machine) must never leak into the test process. netbox_sync.config
# calls load_dotenv() at import time, and config-reload tests would re-read
# it on every reload — so stub it out before any netbox_sync import happens.
dotenv.load_dotenv = lambda *a, **k: None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("NETBOX_URL", "https://netbox.test")
os.environ.setdefault("NETBOX_TOKEN", "dummy-token")
os.environ.setdefault("REDFISH_USER", "dummy")
os.environ.setdefault("REDFISH_PASS", "dummy")
os.environ.setdefault("STORAGE_USER", "dummy")
os.environ.setdefault("STORAGE_PASS", "dummy")
os.environ.setdefault("SWITCH_USER", "dummy")
os.environ.setdefault("SWITCH_PASS", "dummy")
