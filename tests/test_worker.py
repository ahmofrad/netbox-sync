"""Fresh-process runner security and termination tests."""
import json
import os
import threading

from netbox_sync import worker


class FakeLock:
    def __init__(self, renew=True):
        self.renew_result = renew
        self.released = False
        self.acquired = True
    def renew(self):
        return self.renew_result
    def release(self):
        self.released = True
        return True


class FakeProcess:
    def __init__(self, payload=None, returncode=0):
        self.payload = payload or {"main_succeeded": True, "assetexplorer_state": "succeeded", "succeeded": True}
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.lines = ["completed token=fake-log-token\n"]
        self.stdout = iter(self.lines)
    def communicate(self, timeout=None):
        if self.returncode is None:
            raise worker.subprocess.TimeoutExpired("pipeline", timeout)
        return "".join(self.lines), ""
    def wait(self, timeout=None):
        if timeout is not None and self.returncode is None:
            raise worker.subprocess.TimeoutExpired("pipeline", timeout)
        return self.returncode if self.returncode is not None else 0
    def terminate(self):
        self.terminated = True
        self.returncode = -15
    def kill(self):
        self.killed = True
        self.returncode = -9


def test_worker_uses_fresh_process_sanitizes_and_cleans(monkeypatch, tmp_path):
    process = FakeProcess()
    captured = {}
    logs = []
    lock = FakeLock()
    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        path = command[command.index("--result-file") + 1]
        with open(path, "w") as stream:
            json.dump(process.payload, stream)
        return process
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    monkeypatch.setenv("NETBOX_TOKEN", "fake-secret-not-in-arguments")
    result = worker.run_subprocess(lock=lock, log_callback=logs.append, result_directory=tmp_path)

    assert result.succeeded is True
    assert captured["command"][:3] == [worker.sys.executable, "-m", "netbox_sync.pipeline"]
    assert not any("fake-secret-not-in-arguments" in part for part in captured["command"])
    assert captured["env"]["NETBOX_SYNC_WORKER"] == "1"
    assert "fake-log-token" not in "".join(logs)
    assert list(tmp_path.iterdir()) == []
    assert lock.released is True


def test_worker_failure_without_summary_does_not_claim_completion(monkeypatch, tmp_path):
    process = FakeProcess(payload={
        "succeeded": False, "partial": False,
        "failures": [{"stage": "discovery", "message": "collection failed token=fake-summary-canary"}],
    }, returncode=1)
    def popen(command, **kwargs):
        with open(command[command.index("--result-file") + 1], "w") as stream:
            json.dump(process.payload, stream)
        return process
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    result = worker.run_subprocess(lock=FakeLock(), result_directory=tmp_path)
    assert result.status == "failed"
    assert "Synchronization failed" in result.summary
    assert "collection failed" in result.summary
    assert "fake-summary-canary" not in result.summary


def test_worker_sanitizes_nested_result_payload_before_persistence(monkeypatch, tmp_path):
    canary = "fake-nested-result-secret"
    monkeypatch.setenv("NETBOX_TOKEN", canary)
    process = FakeProcess(payload={
        "succeeded": False, "partial": True,
        "summary": "safe summary",
        "families": {"servers": {"status": "partial", "counters": {"failed": 1}}},
        "issues": [{"source": canary, "message": "failed", "occurrences": 1}],
    })
    def popen(command, **kwargs):
        with open(command[command.index("--result-file") + 1], "w") as stream:
            json.dump(process.payload, stream)
        return process
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    result = worker.run_subprocess(lock=FakeLock(), result_directory=tmp_path)
    assert result.status == "partial"
    assert canary not in str(result.snapshot)
    assert result.snapshot["issues"][0]["source"] == "[redacted]"


def test_worker_starts_dedicated_process_group_and_heartbeats(monkeypatch, tmp_path):
    process = FakeProcess(returncode=None)
    process.wait_calls = 0

    def wait(timeout=None):
        process.wait_calls += 1
        if process.wait_calls == 1:
            raise worker.subprocess.TimeoutExpired("pipeline", timeout)
        process.returncode = 0
        return 0

    process.wait = wait
    process.payload = {"succeeded": True}
    captured = {}
    heartbeats = []

    def popen(command, **kwargs):
        captured.update(kwargs)
        with open(command[command.index("--result-file") + 1], "w") as stream:
            json.dump(process.payload, stream)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    result = worker.run_subprocess(lock=FakeLock(), heartbeat_callback=lambda: heartbeats.append(1),
                                   heartbeat_interval=0, result_directory=tmp_path)
    assert result.succeeded is True
    assert heartbeats
    if os.name == "nt":
        assert captured["creationflags"] & worker.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["start_new_session"] is True


def test_termination_targets_process_group_before_escalating(monkeypatch, tmp_path):
    process = FakeProcess(returncode=None)
    signals = []

    def signal_group(target, *, kill=False):
        signals.append(kill)
        if kill:
            process.returncode = -9

    monkeypatch.setattr(worker, "_signal_process_group", signal_group)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    result = worker.run_subprocess(lock=FakeLock(renew=False), heartbeat_interval=0,
                                   result_directory=tmp_path)
    assert result.status == "failed"
    assert signals == [False, True]
    assert process.killed is False


def test_real_heartbeat_thread_ownership_loss_racing_child_exit_fails(monkeypatch, tmp_path):
    class ThreadedLock(FakeLock):
        def __init__(self):
            super().__init__()
            self.ownership_lost = False
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._heartbeat)
            self._thread.start()

        def _heartbeat(self):
            while not self._stop.wait(0.005):
                if not self.renew():
                    self.ownership_lost = True
                    return

        def release(self):
            self._stop.set()
            self._thread.join(timeout=1)
            self.released = True
            return True

    lock = ThreadedLock()
    renewed = threading.Event()
    def renew():
        renewed.set()
        return False
    lock.renew = renew
    process = FakeProcess(returncode=None)

    def wait(timeout=None):
        assert renewed.wait(timeout=1)
        process.returncode = 0
        return 0

    process.wait = wait
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    result = worker.run_subprocess(lock=lock, heartbeat_interval=0.001, result_directory=tmp_path)
    assert result.status == "failed"
    assert "ownership" in result.summary


def test_child_exit_after_background_ownership_loss_is_not_success(monkeypatch, tmp_path):
    process = FakeProcess()
    lock = FakeLock()
    lock.ownership_lost = True
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    result = worker.run_subprocess(lock=lock, result_directory=tmp_path)
    assert result.status == "failed"
    assert result.succeeded is False
    assert "ownership" in result.summary


def test_lock_renewal_backend_error_returns_failed_result(monkeypatch, tmp_path):
    process = FakeProcess(returncode=None)
    lock = FakeLock()
    lock.renew = lambda: (_ for _ in ()).throw(worker.RedisLockError("renew failed token=fake-renew-canary"))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    result = worker.run_subprocess(lock=lock, heartbeat_interval=0, result_directory=tmp_path)
    assert result.status == "failed"
    assert result.succeeded is False
    assert "fake-renew-canary" not in result.summary
    assert process.terminated or process.killed


def test_lock_release_backend_error_does_not_mask_primary_result(monkeypatch, tmp_path):
    process = FakeProcess()
    lock = FakeLock()
    lock.release = lambda: (_ for _ in ()).throw(worker.RedisLockError("release failed token=fake-release-canary"))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: process)
    result = worker.run_subprocess(lock=lock, result_directory=tmp_path)
    assert result.status == "failed"
    assert result.succeeded is False
    assert "fake-release-canary" not in result.summary


def test_worker_timeout_and_returncode_are_failures(monkeypatch, tmp_path):
    process = FakeProcess(returncode=2)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: process)
    result = worker.run_subprocess(lock=FakeLock(), result_directory=tmp_path)
    assert result.succeeded is False
    assert result.status == "failed"
    assert result.returncode == 2


def test_total_timeout_terminates_child(monkeypatch, tmp_path):
    process = FakeProcess(returncode=None)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: process)
    clock = iter((0, 0, 0.5, 1.1))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    result = worker.run_subprocess(lock=FakeLock(), timeout=1, heartbeat_interval=0.4,
                                   result_directory=tmp_path)
    assert result.status == "failed"
    assert "timed out" in result.summary
    assert process.terminated or process.killed


def test_ownership_loss_terminates_child(monkeypatch, tmp_path):
    process = FakeProcess(returncode=None)
    lock = FakeLock(renew=False)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: process)
    result = worker.run_subprocess(lock=lock, heartbeat_interval=0, result_directory=tmp_path)
    assert result.status == "failed"
    assert process.terminated or process.killed
    assert "ownership" in result.summary
    assert lock.released is True
