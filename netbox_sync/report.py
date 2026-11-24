"""Per-run report of device scan/processing failures.

Probes and collectors record categorized failures here (thread-safe); at the
end of a run, :func:`print_summary` emits a compact, grouped summary so the
operator can immediately see which devices need attention and why.
"""
import socket
import threading
from collections import defaultdict

from netbox_sync.config import log

_lock = threading.Lock()
_failures = []  # list of (family, ip, category, reason)

_COUNTERS = ("discovered", "created", "updated", "adopted", "unchanged", "failed", "offline")
_FAMILY_KEYS = {
    "server": "servers", "servers": "servers",
    "storage": "storage",
    "san switch": "san_switches", "san_switches": "san_switches",
    "cisco switch": "cisco_switches", "cisco_switches": "cisco_switches",
    "fortigate": "fortigates", "fortigates": "fortigates",
    "fortiweb": "fortiwebs", "fortiwebs": "fortiwebs",
    "ftd": "ftds", "ftds": "ftds",
    "ruckus": "ruckus",
    "unifi console": "unifi", "unifi": "unifi",
    "hikvision nvr": "hikvision_nvrs", "hikvision_nvrs": "hikvision_nvrs",
    "dahua nvr": "dahua_nvrs", "dahua_nvrs": "dahua_nvrs",
    "uniview nvr": "unv_nvrs", "unv nvr": "unv_nvrs", "unv_nvrs": "unv_nvrs",
    "offline sweep": "offline_sweeps", "offline_sweeps": "offline_sweeps",
}


def canonical_family(family):
    """Translate legacy collector labels to durable family keys."""
    value = str(family or "")
    return _FAMILY_KEYS.get(value.lower(), value)


_LEGACY_FAMILY_LABELS = {
    "servers": "Server", "storage": "Storage", "san_switches": "SAN switch",
    "cisco_switches": "Cisco switch", "fortigates": "FortiGate",
    "fortiwebs": "FortiWeb", "ftds": "FTD", "ruckus": "Ruckus",
    "unifi": "UniFi console", "hikvision_nvrs": "Hikvision NVR",
    "dahua_nvrs": "Dahua NVR", "unv_nvrs": "UNV NVR",
    "offline_sweeps": "Offline sweep",
}


def sanitize_text(value, limit=500):
    """Redact credential-shaped input and bound persisted/logged free text."""
    import re
    import os

    text = str(value or "")
    text = re.sub(r"(?i)(authorization\s*:\s*(?:token|bearer|basic)\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b(token|password|passwd|secret|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "bearer [redacted]", text)
    for name in ("NETBOX_TOKEN", "MCP_NETBOX_TOKEN", "AE_API_KEY"):
        secret = os.getenv(name)
        if secret and len(secret) >= 8 and secret not in {"dummy-token", "unit-test-token"}:
            text = text.replace(secret, "[redacted]")
    return text[: max(1, int(limit))]


class RunReporter:
    """Thread-safe structured run data, independent of console formatting."""

    def __init__(self, max_issues=500, max_message_length=500):
        self._lock = threading.Lock()
        self._failures = []
        self._counters = {key: 0 for key in _COUNTERS}
        self._families = {}
        self._active_family = None
        self._processing_attempts = {}
        self.max_issues = max_issues
        self.max_message_length = max_message_length

    def record_probe_failure(self, family, ip, category, reason):
        family = canonical_family(family)
        with self._lock:
            self._failures[:] = [f for f in self._failures
                                 if not (f[0] == family and f[1] == str(ip))]
            self._failures.append((family, str(ip), category,
                                   sanitize_text(reason, self.max_message_length)))
            self._counters["failed"] += 1
            self._family(family)["counters"]["failed"] += 1

    def record_processing_failure(self, family, ip, reason, count_attempt=False):
        family = canonical_family(family)
        with self._lock:
            key = (family, str(ip), "no data", sanitize_text(reason, self.max_message_length))
            self._processing_attempts[key] = self._processing_attempts.get(key, 0) + 1
            if count_attempt:
                self._counters["failed"] += 1
            if not any(f[0] == family and f[1] == ip and f[2] == "no data"
                       for f in self._failures):
                self._failures.append(key)
                self._family(family)["counters"]["failed"] += 1
                if not count_attempt:
                    self._counters["failed"] += 1

    def record_device_outcome(self, outcome, device_id=None, family=None, count=1):
        if outcome not in _COUNTERS:
            raise ValueError(f"unknown device outcome: {outcome}")
        with self._lock:
            self._counters[outcome] += count
            family = family or self._active_family
            if family:
                self._family(family)["counters"][outcome] += count

    def record_adoption(self, device_id=None, family=None):
        self.record_device_outcome("adopted", device_id, family)

    def start_family(self, family, display_label=None):
        family = canonical_family(family)
        with self._lock:
            self._family(family, display_label)
            self._active_family = family

    def finish_family(self, family, status=None):
        family = canonical_family(family)
        with self._lock:
            value = self._family(family)
            if status is None:
                aliases = {family}
                if family.endswith("_nvrs"):
                    aliases.add(f"{family[:-5].capitalize()} NVR")
                failed = value["counters"]["failed"] or any(
                    issue[0] in aliases for issue in self._failures)
                status = "failed" if failed and not any(
                    value["counters"][key] for key in
                    ("created", "updated", "adopted", "unchanged")) else \
                    "partial" if failed else "succeeded"
            value["status"] = status
            if self._active_family == family:
                self._active_family = None

    def _family(self, key, display_label=None):
        return self._families.setdefault(key, {
            "display_label": display_label or key,
            "status": "running",
            "counters": {name: 0 for name in _COUNTERS},
        })

    def finish_active_families(self):
        """Close families when a top-level exception bypasses normal loops."""
        with self._lock:
            families = [(key, value) for key, value in self._families.items()
                        if value["status"] == "running"]
        for family, value in families:
            failed = value["counters"]["failed"] or any(
                issue[0] == family for issue in self._failures)
            self.finish_family(family, "failed" if not failed else None)

    def clear(self):
        with self._lock:
            self._failures.clear()
            self._processing_attempts.clear()
            self._counters = {key: 0 for key in _COUNTERS}
            self._families.clear()
            self._active_family = None

    def snapshot(self):
        from collections import defaultdict
        with self._lock:
            grouped = defaultdict(int)
            for family, source, category, message in self._failures:
                key = (family, source, category, message)
                grouped[key] += self._processing_attempts.get(key, 1)
            issues = [{
                "family": family, "source": source, "category": category,
                "severity": "error", "message": message, "occurrences": occurrences,
            } for (family, source, category, message), occurrences in grouped.items()]
            return {
                "counters": dict(self._counters),
                "families": {key: {"display_label": value["display_label"],
                                    "status": value["status"],
                                    "counters": dict(value["counters"])}
                              for key, value in self._families.items()},
                "failures": list(self._failures),
                "issues": issues[: self.max_issues],
            }

    def print_summary(self, found):
        _print_summary(self.snapshot(), found)


_current_reporter = RunReporter()


class _FailureCompatibility:
    """Keep the legacy list patchable while structured state remains canonical."""
    def __setitem__(self, key, value):
        if key != slice(None, None, None):
            raise TypeError("legacy failure storage supports full replacement only")
        _current_reporter.clear()
        for family, ip, category, reason in value:
            if category == "no data":
                _current_reporter.record_processing_failure(family, ip, reason)
            else:
                _current_reporter.record_probe_failure(family, ip, category, reason)

    def __iter__(self):
        return iter(_current_reporter.snapshot()["failures"])

    def __len__(self):
        return len(_current_reporter.snapshot()["failures"])

    def __getitem__(self, item):
        return _current_reporter.snapshot()["failures"][item]

    def __eq__(self, other):
        return list(self) == other


_failures = _FailureCompatibility()
_legacy_failures = None


def _legacy_records():
    return _failures if _legacy_failures is None else _legacy_failures


def get_reporter():
    return _current_reporter


class CollectorAuthError(RuntimeError):
    """Device rejected the credentials."""

class CollectorConnectionError(RuntimeError):
    """Device unreachable at the TCP/TLS layer."""

class CollectorNotFoundError(RuntimeError):
    """Device answered but reported no usable data (empty inventory, etc.)."""


def classify_error(exc):
    """Map a low-level exception to a short, operator-readable reason."""
    if exc is None:
        return "unknown error"
    if isinstance(exc, CollectorAuthError):
        return "authentication failure"
    if isinstance(exc, CollectorConnectionError):
        return f"connection error: {exc}"
    if isinstance(exc, CollectorNotFoundError):
        return str(exc)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        if status == 401:
            return "authentication failure (HTTP 401)"
        if status == 403:
            return "forbidden / insufficient permissions (HTTP 403)"
        if status == 408:
            return "HTTP timeout (HTTP 408)"
        if status >= 500:
            return f"device API error (HTTP {status})"
        return f"HTTP {status} from device API"
    mod = type(exc).__module__ or ""
    if isinstance(exc, socket.timeout) or "Timeout" in type(exc).__name__ \
            or "timed out" in str(exc).lower():
        return "timeout"
    if isinstance(exc, (ConnectionRefusedError, socket.error)) \
            and "refused" in str(exc).lower():
        return "connection refused"
    if "requests.exceptions" in mod or mod.startswith("requests"):
        name = type(exc).__name__
        if "SSLError" in name:
            return f"TLS/SSL error: {exc}"
        if "ConnectionError" in name:
            return f"connection error: {exc}"
        return f"{name}: {exc}"
    if isinstance(exc, PermissionError):
        return f"permission denied: {exc}"
    return f"{type(exc).__name__}: {exc}"


def record_probe_failure(family, ip, category, reason):
    global _legacy_failures
    if isinstance(_failures, list):
        _legacy_failures = _failures
        with _lock:
            _failures[:] = [f for f in _failures
                            if not (f[0] == family and f[1] == ip)]
            _failures.append((family, ip, category, sanitize_text(reason)))
    else:
        _legacy_failures = None
        _current_reporter.record_probe_failure(family, ip, category, reason)


def record_processing_failure(family, ip, reason):
    global _legacy_failures
    if isinstance(_failures, list):
        _legacy_failures = _failures
        with _lock:
            if not any(f[0] == family and f[1] == ip and f[2] == "no data"
                       for f in _failures):
                _failures.append((family, ip, "no data", sanitize_text(reason)))
        _current_reporter.record_processing_failure(family, ip, reason)
        get_reporter().record_processing_failure(family, ip, reason)
    else:
        _legacy_failures = None
        get_reporter().record_processing_failure(family, ip, reason)


def clear():
    _current_reporter.clear()
    if _legacy_failures is not None:
        _legacy_failures.clear()


def _grouped():
    by_key = defaultdict(list)
    for family, ip, category, reason in _legacy_records():
        by_key[(_LEGACY_FAMILY_LABELS.get(family, family), category, reason)].append(ip)
    return by_key


def _print_summary(snapshot, found):
    """End-of-run report: per-family processed counts + grouped failures."""
    log("INFO", "=" * 60)
    log("INFO", "SCAN SUMMARY")
    log("INFO", "=" * 60)
    labels = {
        "servers": "Servers", "storage": "Storage",
        "san_switches": "SAN switches", "cisco_switches": "Cisco switches",
        "fortigates": "FortiGates", "fortiwebs": "FortiWeb WAFs",
        "ftds": "Cisco FTDs", "ruckus": "Ruckus",
        "unifi": "UniFi consoles", "hikvision_nvrs": "Hikvision NVRs",
        "dahua_nvrs": "Dahua NVRs", "unv_nvrs": "Uniview NVRs",
    }
    for key, label in labels.items():
        log("INFO", f"  [OK] {label}: {len(found.get(key, []))} processed")
    by_key = defaultdict(list)
    for family, ip, category, reason in snapshot["failures"]:
        by_key[(family, category, reason)].append(ip)
    if not by_key:
        log("INFO", "  No scan failures — every probed device was processed.")
        return
    log("WARN", f"  {sum(len(v) for v in by_key.values())} device(s) need attention:")
    for (family, category, reason), ips in sorted(by_key.items()):
        tag = "UNREACHABLE" if category == "unreachable" else "NO DATA"
        ip_list = ", ".join(sorted(ips))
        log("WARN", f"  [{tag}] {family} — {reason} ({len(ips)}): {ip_list}")


def print_summary(found):
    _current_reporter.print_summary(found)
