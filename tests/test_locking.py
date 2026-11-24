"""Redis execution ownership tests (no external Redis required)."""
import threading
import time

import pytest

from netbox_sync.locking import RedisExecutionLock, RedisLockError


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False

    def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        if nx and key in self.values:
            return False
        self.values[key] = (value, px)
        return True

    def eval(self, script, key_count, key, *args):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        if "del" not in script:
            return int(self.values.get(key, (b"0",))[0] == args[0])
        if self.values.get(key, (None,))[0] != args[0]:
            return 0
        del self.values[key]
        return 1

    def pexpire(self, key, ttl):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        if key not in self.values:
            return False
        self.values[key] = (self.values[key][0], ttl)
        return True


def test_acquire_conflict_owner_release_and_heartbeat():
    client = FakeRedis()
    first = RedisExecutionLock(client, heartbeat_interval=0.01)
    second = RedisExecutionLock(client)
    assert first.acquire() is True
    assert second.acquire() is False
    deadline = time.monotonic() + 0.1
    while first.renew_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert first.renew_count > 0
    assert first.release() is True
    assert second.acquire() is True
    second.stop()


def test_owner_cannot_release_another_owner():
    client = FakeRedis()
    first = RedisExecutionLock(client)
    second = RedisExecutionLock(client)
    first.acquire()
    assert second.release() is False
    assert client.values
    first.stop()


def test_redis_failure_fails_closed_and_lost_ownership_stops_heartbeat():
    client = FakeRedis()
    lock = RedisExecutionLock(client, heartbeat_interval=100)
    client.fail = True
    with pytest.raises(RedisLockError):
        lock.acquire()
    client.fail = False
    assert lock.acquire() is True
    client.values.clear()
    assert lock.renew() is False
    assert lock.ownership_lost is True
    assert lock._heartbeat_thread is None or not lock._heartbeat_thread.is_alive()


def test_release_after_exception_and_interrupt():
    client = FakeRedis()
    lock = RedisExecutionLock(client)
    with lock:
        pass
    assert not client.values
    with pytest.raises(KeyboardInterrupt):
        with lock:
            raise KeyboardInterrupt
    assert not client.values
