"""Run one pipeline in a fresh process with sanitized, temporary results."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from netbox_sync.locking import RedisLockError
from netbox_sync.report import sanitize_text


@dataclass
class WorkerResult:
    status: str
    succeeded: bool
    partial: bool = False
    returncode: int | None = None
    summary: str = ""
    snapshot: dict = field(default_factory=dict)


def _sanitize_payload(value):
    """Recursively clean untrusted worker output before durable persistence."""
    known_secrets = [os.environ.get(name, "") for name in
                     ("NETBOX_TOKEN", "MCP_NETBOX_TOKEN", "AE_API_KEY")]

    def clean_text(text):
        clean = sanitize_text(text)
        for secret in known_secrets:
            if secret and len(secret) >= 8 and secret not in {"dummy-token", "unit-test-token"}:
                clean = clean.replace(secret, "[redacted]")
        return clean

    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return clean_text(value)
    return value


def _signal_process_group(process, *, kill=False):
    if os.name == "nt":
        # A new process group isolates console Ctrl events from the RQ worker.
        if not kill:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.kill()
        return
    os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)


def _terminate(process):
    if process.returncode is not None:
        return
    try:
        _signal_process_group(process)
    except (AttributeError, OSError, ProcessLookupError):
        # Lightweight test doubles and already-exited children may not expose
        # a real process group; retain immediate-child cleanup as a fallback.
        process.terminate()
    try:
        process.wait(timeout=10)
    except Exception:
        try:
            _signal_process_group(process, kill=True)
        except (AttributeError, OSError, ProcessLookupError):
            process.kill()
        process.wait(timeout=5)


def run_subprocess(*, lock, log_callback=None, heartbeat_callback=None, timeout=86400,
                   include_assetexplorer=True, run_id=None, result_directory=None,
                   heartbeat_interval=1.0):
    """The caller supplies an already-held shared lock; release is guaranteed."""
    output_name = None
    process = None
    ownership_lost = threading.Event()
    cleanup_error = None
    try:
        fd, output_name = tempfile.mkstemp(prefix="netbox-sync-result-", suffix=".json",
                                           dir=result_directory)
        os.close(fd)
        try:
            os.chmod(output_name, 0o600)
        except OSError:
            pass
        command = [sys.executable, "-m", "netbox_sync.pipeline", "--worker",
                   "--result-file", output_name]
        if not include_assetexplorer:
            command.append("--skip-assetexplorer")
        environment = os.environ.copy()
        environment["NETBOX_SYNC_WORKER"] = "1"
        if run_id:
            environment["NETBOX_SYNC_RUN_ID"] = str(run_id)
        process_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                           if os.name == "nt" else {"start_new_session": True})
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, env=environment,
                                   **process_options)

        def stream_output():
            for line in process.stdout:
                clean = sanitize_text(line.rstrip())
                if clean and log_callback:
                    log_callback(clean)

        stream = threading.Thread(target=stream_output, daemon=True)
        stream.start()
        if heartbeat_callback is not None:
            heartbeat_callback()
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate(process)
                    break
                try:
                    process.wait(timeout=min(heartbeat_interval, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat_callback is not None:
                        heartbeat_callback()
                    try:
                        renewed = lock.renew()
                    except RedisLockError:
                        renewed = False
                    if not renewed:
                        ownership_lost.set()
                        _terminate(process)
                        break
        finally:
            stream.join(timeout=5)

        if getattr(lock, "ownership_lost", False):
            ownership_lost.set()
            _terminate(process)
        if timed_out:
            return WorkerResult("failed", False, returncode=process.returncode,
                                summary="Synchronization timed out")
        if ownership_lost.is_set():
            return WorkerResult("failed", False, returncode=process.returncode,
                                summary="Execution lock ownership was lost; synchronization stopped")
        try:
            with open(output_name, encoding="utf-8") as stream:
                payload = _sanitize_payload(json.load(stream))
        except (OSError, ValueError) as exc:
            summary = ("Synchronization subprocess failed" if process.returncode != 0 else
                       f"Synchronization result unavailable: {type(exc).__name__}")
            return WorkerResult("failed", False, returncode=process.returncode,
                                summary=summary)
        succeeded = bool(payload.get("succeeded"))
        partial = bool(payload.get("partial"))
        summary = payload.get("summary")
        if not summary:
            failures = payload.get("failures") or []
            if failures:
                summary = f"Synchronization {'partially completed' if partial else 'failed'}: {failures[0].get('message', 'stage failed')}"
            else:
                summary = "Synchronization completed successfully" if succeeded else "Synchronization failed"
        return WorkerResult("succeeded" if succeeded else "partial" if partial else "failed",
                            succeeded, partial, process.returncode,
                            sanitize_text(summary), payload)
    except KeyboardInterrupt:
        if process is not None:
            _terminate(process)
        return WorkerResult("cancelled", False, returncode=process.returncode if process else None,
                            summary="Synchronization cancelled")
    finally:
        if process is not None and process.returncode is None:
            _terminate(process)
        if output_name:
            try:
                os.remove(output_name)
            except OSError:
                pass
        try:
            lock.release()
        except RedisLockError as exc:
            cleanup_error = WorkerResult(
                "failed", False, returncode=process.returncode if process else None,
                summary=sanitize_text(f"Execution lock cleanup failed: {exc}"))
    if cleanup_error is not None:
        return cleanup_error
