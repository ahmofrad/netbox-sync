"""Shared Redis execution ownership with no silent local-lock fallback."""
import contextlib
import os
import secrets
import threading


class RedisLockError(RuntimeError):
    """Redis ownership cannot be established or safely maintained."""


class RedisExecutionLock:
    def __init__(self, client, key="netbox-sync:execution", ttl_seconds=86400,
                 heartbeat_interval=None):
        self.client = client
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.owner = secrets.token_urlsafe(32)
        self.heartbeat_interval = heartbeat_interval or min(300, ttl_seconds / 3)
        self._held = False
        self._stop = threading.Event()
        self._heartbeat_thread = None
        self.ownership_lost = False
        self.renew_count = 0

    def acquire(self):
        try:
            acquired = self.client.set(self.key, self.owner, nx=True,
                                       px=self.ttl_seconds * 1000)
        except Exception as exc:
            raise RedisLockError("Execution lock backend unavailable") from exc
        if not acquired:
            return False
        self._held = True
        self.ownership_lost = False
        self._stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat,
                                                   name="netbox-sync-lock-heartbeat",
                                                   daemon=True)
        self._heartbeat_thread.start()
        return True

    def renew(self):
        if not self._held or self.ownership_lost:
            return False
        try:
            renewed = self.client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end",
                1, self.key, self.owner, self.ttl_seconds * 1000)
        except Exception as exc:
            self._lose_ownership()
            raise RedisLockError("Execution lock ownership could not be renewed") from exc
        if not renewed:
            self._lose_ownership()
            return False
        self.renew_count += 1
        return True

    def _lose_ownership(self):
        self.ownership_lost = True
        self._held = False
        self._stop.set()
        thread = self._heartbeat_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=min(self.heartbeat_interval + 0.1, 1))

    def release(self):
        self._stop.set()
        thread = self._heartbeat_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=min(self.heartbeat_interval + 0.1, 1))
        if not self._held:
            return False
        self._held = False
        try:
            return bool(self.client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, self.key, self.owner))
        except Exception as exc:
            raise RedisLockError("Execution lock could not be safely released") from exc
        finally:
            self.stop()

    def stop(self):
        self._stop.set()
        self._held = False

    def _heartbeat(self):
        while not self._stop.wait(self.heartbeat_interval):
            try:
                if not self.renew():
                    return
            except RedisLockError:
                return

    def __enter__(self):
        if not self.acquire():
            raise RedisLockError("Another synchronization run owns the execution lock")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


def redis_from_url(url=None):
    """Construct the process-local client; never persist or log its URL."""
    import redis
    return redis.Redis.from_url(url or os.environ["SYNC_REDIS_URL"])


@contextlib.contextmanager
def configured_execution_lock():
    """Explicit opt-in for legacy workflows; backend errors fail closed."""
    lock = RedisExecutionLock(redis_from_url())
    if not lock.acquire():
        raise RedisLockError("Another synchronization run owns the execution lock")
    try:
        yield lock
    finally:
        lock.release()
