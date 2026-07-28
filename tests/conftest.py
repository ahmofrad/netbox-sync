"""Shared pytest setup.

Makes the repo root importable and provides dummy credentials so that
``import sync_all_to_netbox`` succeeds without a real .env file (the module
validates credentials at import time). ``setdefault`` never overrides a
real environment.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("NETBOX_URL", "https://netbox.test")
os.environ.setdefault("NETBOX_TOKEN", "dummy-token")
os.environ.setdefault("REDFISH_USER", "dummy")
os.environ.setdefault("REDFISH_PASS", "dummy")
os.environ.setdefault("STORAGE_USER", "dummy")
os.environ.setdefault("STORAGE_PASS", "dummy")
os.environ.setdefault("SWITCH_USER", "dummy")
os.environ.setdefault("SWITCH_PASS", "dummy")
