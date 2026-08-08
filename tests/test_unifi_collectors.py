"""Tests for the UniFi collector probe behavior (in-memory fakes)."""
import requests

import netbox_sync.collectors.unifi as mod


def _probe_fakes(monkeypatch, session_factory):
    """Port open + /status up; UniFiSession replaced by session_factory."""
    sleeps = []
    monkeypatch.setattr(mod, "is_port_open", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "_status",
                        lambda ip, port: {"server_version": "10.2",
                                          "uuid": "u-1"})
    monkeypatch.setattr(mod, "UniFiSession", session_factory)
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_probe_unifi_returns_none_on_login_rejection_without_retry(monkeypatch):
    """A RuntimeError from login is a config error: no retry, no sleep."""
    class RejectingSession:
        def __init__(self, ip):
            pass
        def login(self):
            raise RuntimeError("UniFi login failed: HTTP 401")
        def logout(self):
            pass

    sleeps = _probe_fakes(monkeypatch, RejectingSession)
    assert mod.probe_unifi("10.0.0.1", retries=2, retry_delay=3) is None
    assert sleeps == []


def test_probe_unifi_retries_transient_errors(monkeypatch):
    """Transient network errors (e.g. ConnectTimeout) still retry."""
    attempts = []

    class FlakySession:
        def __init__(self, ip):
            pass
        def login(self):
            attempts.append(1)
            raise requests.exceptions.ConnectTimeout()
        def logout(self):
            pass

    _probe_fakes(monkeypatch, FlakySession)
    assert mod.probe_unifi("10.0.0.1", retries=2, retry_delay=0) is None
    assert len(attempts) == 2
