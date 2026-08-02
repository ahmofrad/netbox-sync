# Cron-mode operation (remove built-in scheduler) — design spec

**Date:** 2026-07-29
**Status:** Approved
**Scope:** Remove the `schedule` library and loop; the script runs one full sync and exits. Add cron hardening: exit codes, .env cwd-fallback, overlap lockfile. Scheduling is the operator's job (cron/systemd/Task Scheduler).

---

## 1. Decisions

| Question | Decision |
|----------|----------|
| Scheduler | Removed entirely; `schedule` dependency dropped |
| Exit codes | `0` success · `1` config error / lock conflict / unexpected error · `130` Ctrl+C |
| .env under cron | dotenv upward search (works already) + explicit package-relative fallback when `NETBOX_URL` unset |
| Overlap guard | Lockfile `netbox-sync.lock` in repo root (gitignored); exclusive create; `atexit` release; existing fresh lock → exit 1; lock older than 24 h = stale → replaced |
| Entry shape | Logic in `netbox_sync/main.py` (`main() -> int`, testable); `sync_all_to_netbox.py` becomes a 3-line shim |

## 2. Components

**`netbox_sync/main.py`**
- `_acquire_lock(path=LOCK_FILE) -> bool` — `O_CREAT|O_EXCL` create with PID inside; registers `atexit` release; stale (> `LOCK_MAX_AGE_SECONDS = 24h`) → WARN + replace.
- `main() -> int` — `_validate_config()` (RuntimeError → ERROR log, 1) → lock (conflict → ERROR log, 1) → `run_sync()` (KeyboardInterrupt → 130, unexpected → traceback + 1, success → 0).

**`netbox_sync/config.py`** — after `load_dotenv()`: if `NETBOX_URL` unset, `load_dotenv(<package>/../.env)`.

**`sync_all_to_netbox.py`** — `sys.exit(main())` shim.

**`requirements.txt`** — drop `schedule`.

**`.gitignore`** — add `netbox-sync.lock`.

**README (EN+FA)** — "What it does" bullet reworded; `Running` section rewritten around single-run + cron example (`0 0,12 * * *` mirrors the old cadence); lockfile documented; `schedule` removed from dependency lists (and `netmiko` added where missing); log sample updated; service section replaced by the cron section; the "run exactly one instance" warning replaced by lockfile enforcement.

## 3. Testing

- `_acquire_lock`: acquire/release, conflict while held, stale replacement (mtime older than 24 h), using `tmp_path`.
- `main()`: 1 on config error, 1 on lock conflict, 0 on success (run_sync invoked once), 1 on unexpected exception — all with monkeypatched collaborators.
- The .env fallback is a no-op under tests (conftest stubs `dotenv.load_dotenv`); verified manually.
- Existing suite must stay green (nothing else changes).

## 4. Non-goals (YAGNI)

- No systemd unit files shipped (cron example in README suffices).
- No PID-liveness checking (age-based staleness is simpler and platform-independent).
- No run metrics/timing reporting, no `--once` flag (run-once is the only mode).
