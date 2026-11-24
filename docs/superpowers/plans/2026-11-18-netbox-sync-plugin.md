# Netbox-sync Plugin, Operations Dashboard, and MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Use `test-driven-development` for every implementation task and `verification-before-completion` before declaring the work complete.

**Goal:** Build one `netbox-sync-plugin` Python distribution containing the existing synchronization engine, a NetBox 4.5.1 plugin with scheduled/manual synchronization and an Operations dashboard, and the existing read-only MCP service, without changing NetBox core or existing reconciliation semantics.

**Architecture:** Keep `pynetbox` reconciliation intact. Run the engine in a fresh subprocess per worker/CLI pipeline to isolate import-time configuration and caches. Use a plugin-owned `SyncState` singleton as the recurring-job anchor and durable run-history models. NetBox and its management command use NetBox's task Redis connection for a shared ownership lock; legacy standalone scripts retain their file lock. The dashboard is a permission-protected Django page. MCP remains a separate GET-only service with its own `MCP_NETBOX_TOKEN`.

**Tech Stack:** Python 3.10+, Django/NetBox 4.5.1 plugin APIs, `JobRunner`, `enqueue_once()`, `django_rq`, Redis, RQ, existing `pynetbox` engine, Django templates plus small plugin-owned CSS/JS assets, FastMCP (`mcp<2`), setuptools, pytest.

**Approved specification:** `docs/superpowers/specs/2026-11-18-netbox-sync-plugin-design.md`

**Visual reference:** `.superpowers/brainstorm/475-1794986208/content/operations-dashboard-soft-v2.html` (local only; illustrative values are not fixtures or expected live results).

---

## Non-negotiable constraints

- Do not modify NetBox core, rewrite reconciliation against Django ORM, or change identity, adoption, HA/stack, offline, collision, inventory, AssetExplorer, or cabling behavior except for the two named reliability fixes.
- No network discovery or MCP queries in browser request/response cycles. No production NetBox writes during unit/integration test setup.
- Keep `sync_all_to_netbox.py`, `sync_assetexplorer.py`, `sync_single_device.py`, and `run_daily_sync.sh` usable. Main sync success remains the precondition for AssetExplorer.
- Do not add UI deletion, wipe, manual-offline, conflict-repair, or MCP write/job-enqueue capabilities. Do not reintroduce NVR HDD scanning; NVR disks remain ManageEngine-only.
- Never persist credentials, bearer/basic tokens, cookies, credential-bearing URLs, raw authentication responses, or environment dumps. Never put a secret in documentation or tests. Issue messages are sanitized and bounded before persistence.
- Use `MCP_NETBOX_TOKEN` for the packaged MCP service; it must not silently inherit the synchronization `NETBOX_TOKEN`. Require the existing read-only `write_enabled=False` token configuration.
- Tests mock collectors, NetBox API calls, subprocess execution, and locks unless a task explicitly targets a disposable NetBox 4.5.1 instance.
- At planning time the existing suite collected 377 tests before discovery stopped at `netbox-mcp/e2e_test.py` because local `mcp` is unavailable. Do not weaken or delete tests to hide that dependency error; use the hermetic collection configuration in Task 1.

## Fixed shared contracts

These interfaces are deliberate; use them consistently across tasks.

### Distribution and entry points

- Distribution: `netbox-sync-plugin`; packages: `netbox_sync`, `netbox_sync_plugin`, `netbox_sync_mcp`.
- Plugin app: `netbox_sync_plugin`; base URL: `/plugins/netbox-sync/`; Operations URL: `/plugins/netbox-sync/operations/`.
- Dedicated RQ queue: `netbox_sync_plugin.sync` (NetBox prepends the app name to `PluginConfig.queues = ['sync']`).
- Console scripts: `netbox-sync = netbox_sync.pipeline:main`, `netbox-sync-mcp = netbox_sync_mcp.server:main`.
- Management command: `python manage.py netbox_sync run`, with `--skip-assetexplorer` and `--lock-timeout 0` supported. `schedule` installs/updates the native recurring job; `cleanup` performs bounded history cleanup.

### Runtime and lock

- `netbox_sync.pipeline.run_pipeline(*, include_assetexplorer=True, reporter=None, logger=None) -> PipelineResult` executes main sync, then AssetExplorer only when main succeeds. If AE is unconfigured, record `skipped` rather than an error. Existing stage functions and outcome semantics remain authoritative.
- `PipelineResult` is a small dataclass: `main_succeeded`, `assetexplorer_state` (`succeeded|failed|skipped`), `assetexplorer_succeeded: bool | None`, `succeeded` (all requested/configured stages succeeded), `partial` (some completed work plus a failure), `failures`, `families`.
- `netbox_sync.locking.RedisExecutionLock(client, key='netbox-sync:execution', ttl_seconds=86400)` uses `SET NX PX`, a cryptographically random owner token, a background heartbeat, and compare-and-delete Lua release. `acquire() -> bool`, `renew() -> bool`, `release() -> bool`; loss of ownership stops heartbeat and is surfaced as an error. Use `get_connection('netbox_sync_plugin.sync')` in plugin code, so NetBox supplies Redis settings and no Redis password is duplicated in plugin configuration. Standalone Redis opt-in uses `SYNC_REDIS_URL` read only by the process; never write it to plugin models, logs, or docs.
- A held lock never gets deleted merely because its owner process disappeared; TTL expiry handles crash recovery. The worker/management command does not silently fall back to a local file lock if Redis fails. Legacy `sync_all_to_netbox.py` retains `_acquire_lock()` file-lock behavior and its current return codes by default. For controlled cutover, an explicit server-side `SYNC_LOCK_BACKEND=redis` makes legacy full/AE/single-device entry points use the same Redis lock; it must fail closed rather than fall back when Redis is unavailable.
- A fresh subprocess per pipeline isolates existing import-time settings and per-process caches. The subprocess receives inherited server-side environment plus only nonsecret run identifiers; do not pass credentials on the command line or result file. `netbox_sync.worker` owns subprocess invocation, sanitized streaming logs, and a sanitized JSON result in a `0600` temporary file outside the repository. Always remove the temporary result in `finally`. If heartbeat ownership is lost, terminate the child process group promptly, wait briefly, then kill it if needed; record a failed run and never let it continue writing.

### Persistence

- `SyncState` singleton (`key='default'`, `enforce unique key`) holds no credentials: `schedule_enabled`, `interval_minutes` (default 1440), `last_heartbeat`, `last_enqueue_error`, `retention_runs` (default 90), `max_issues_per_run` (default 500), `issue_message_max_length` (default 500). It implements `JobsMixin` only as needed for NetBox's job association; it is not exposed as an editable device/configuration object. Its unique singleton instance must exist before system-job registration/scheduling; provide idempotent `get_or_create(key='default')` initialization in `ready()` without a migration-time network or device operation.
- `SyncRun`: optional self-FK `parent`, optional FK to `core.Job`, optional `requester`, `stage` (`pipeline|discovery|assetexplorer`), `trigger` (`scheduled|manual|cli`), `status` (`queued|running|succeeded|partial|failed|cancelled`), timestamps, `duration_seconds`, `plugin_version`, `source_revision`, seven integer counters (`discovered`, `created`, `updated`, `adopted`, `unchanged`, `failed`, `offline`), `assetexplorer_state`, and bounded sanitized `summary`. Use normal Django models for private operational data, not public device edit pages.
- `SyncFamilyResult`: FK run, `family_key`, `display_label`, `status`, `started_at`, `finished_at`, and the same seven counters.
- `SyncIssue`: FK run, optional FK family result, optional FK `device`, `category`, `severity`, bounded `source`, bounded sanitized `message`, `occurrences`. Aggregate by `(family, category, severity, source, message)` up to the configured run cap; never retain raw responses.
- Plain status/model fields are sufficient; avoid adding GraphQL, REST, changelog, bulk-edit, or public CRUD surfaces.
- Permissions: `netbox_sync_plugin.view_syncrun` gates Operations and private history. `netbox_sync_plugin.run_sync` is a custom permission on `SyncRun` and gates enqueueing. Navigation uses both as appropriate; server-side checks remain authoritative. Device queries use `Device.objects.restrict(request.user, 'view')`.

### Structured reporting

- `netbox_sync.report.RunReporter` is a thread-safe, per-process collector with `record_probe_failure`, `record_processing_failure`, `record_device_outcome`, `start_family`, `finish_family`, and `snapshot()`. Preserve existing `record_probe_failure()`, `record_processing_failure()`, `clear()`, `_grouped()`, and `print_summary()` signatures as wrappers around the current reporter.
- `RunReporter.snapshot()` returns the same counter/family/failure data consumed by CLI JSON and plugin persistence. A run context is created at pipeline start; no prior run's data is inherited because every pipeline is a fresh process.
- Add an internal `get_netbox().dcim.devices` proxy only for device create/update reporting, preserving all normal endpoint attributes and methods. `create()` reports `created`; `update()` reports `updated` only when the submitted device values actually differ from current values, otherwise `unchanged`. Adoption is counted explicitly at existing successful adoption branches, not guessed from log messages. Instrument all device create/update sites, including `sync.py`, `netbox.py`, and `assetexplorer_sync.py`. Do not count component/interface writes as device writes.
- Failure and unchanged semantics must not be inferred from log strings. Preserve exceptions and existing control flow. Tests use fake records/endpoints with field dictionaries as well as pynetbox-like attributes.

### Offline persistence and health

- Add device custom field `scan_misses` (integer, default 0) through existing `CUSTOM_FIELDS`/`ensure_custom_fields()`. `_offline_sweep()` passes the device record as a seventh optional argument to `_check_offline()`; existing six-argument callers retain their behavior. A missing custom field means zero for migration compatibility.
- `_check_offline(ip, live_ips, dev_id, dev_name, mark_fn, label, device=None)` retains its original six-argument signature compatibility and threshold behavior. When `device` is supplied, its persisted count is authoritative; the legacy in-memory count remains only for old direct callers/tests. On a hit, reset `scan_misses=0`; on a miss below threshold, persist `scan_misses=misses`; on threshold, mark offline and reset to 0. Update only this custom field, merge with existing custom-field data, and preserve disabled-family and NVR manufacturer scoping. If the state update fails, record an operational issue and do not mark offline early. Add reset-on-hit coverage in every device's normal successful reconciliation path, not only in the sweep, so rediscovered devices recover their miss state.
- Managed-health scope is the OR of existing `custom_field_data` flags: `redfish_enabled`, `storage_enabled`, `san_switch_enabled`, `cisco_enabled`, `fortigate_enabled`, `fortiweb_enabled`, `ftd_enabled`, `wlc_enabled`, `unifi_enabled`, `nvr_enabled`, `wap_enabled`, `cam_enabled`.
- Management-IP fields in order: `bmc_ip`, `storage_ip`, `san_switch_ip`, `cisco_ip`, `fortigate_ip`, `fortiweb_ip`, `ftd_ip`, `wlc_ip`, `unifi_ip`, `nvr_ip`, `cam_ip`. `wap_enabled` uses primary IPv4 only. A missing management IP means no `primary_ip4` and no nonblank applicable field. Use Python `_invalid_serial()` for hardware identity checks.
- Health details: offline, missing management IP, invalid/missing serial, duplicate valid serial, duplicate nonblank asset tag. Do not invent extra name/site/role constraints. NetBox's unique asset-tag constraint normally prevents new duplicate tags; legacy duplicate data is still reported if present.
- Chart precedence: offline first; otherwise attention if any enabled detail fails; otherwise healthy. Chart and cards share one evaluated snapshot. Detailed checks may overlap. Health links use documented native filters such as `cf_redfish_enabled=True` for scope and `id=<id>` lists for computed/duplicate subsets; never embed serial/tag values in query strings.
- `netbox_sync_plugin.health.InventoryHealth.from_queryset(queryset)` returns `available`, `total`, `offline`, `attention`, `healthy`, per-check counts, and per-check device IDs/links. Query or evaluation failure returns `available=False` and a safe generic message, not zero.

---

## Task 1 — Hermetic baseline, package skeleton, and regression characterization

**Files:**
- Create: `pyproject.toml`, `netbox_sync_mcp/__init__.py`, `netbox_sync_mcp/server.py`, `netbox_sync_mcp/netbox_api.py`, `tests/mcp/__init__.py`, `tests/mcp/test_new_tools.py`
- Modify: `netbox-mcp/server.py`, `netbox-mcp/netbox_api.py`, `requirements-dev.txt`
- Test: `tests/test_pipeline.py`, `tests/test_nvr_regressions.py`

- [ ] **Step 1: Create an isolated development environment if the existing `.venv` is not usable.** Install `requirements-dev.txt`, `requirements.txt`, `mcp<2`, `pytest`, and build tooling only into `.venv`; do not install globally. Do not copy any real `.env` into the environment.
- [ ] **Step 2: Add hermetic test collection.** Move MCP unit tests to `tests/mcp/` and make their imports package-relative with the existing FastMCP decorator stub. Replace the hard-coded absolute path and absolute source-file reads with `pathlib.Path(__file__)` package paths. Mark `netbox-mcp/e2e_test.py` with `pytestmark = pytest.mark.skipif(os.getenv('RUN_MCP_E2E') != '1', ...)` before network/client imports, so collection never needs a live service or real token.
- [ ] **Step 3: Move MCP source without changing tools yet.** Copy `server.py` and `netbox_api.py` into `netbox_sync_mcp`, changing only `import netbox_api as nb` to `from . import netbox_api as nb`. Keep legacy `netbox-mcp/server.py` as a thin compatibility launcher and `netbox-mcp/netbox_api.py` as a compatibility re-export. Use `netbox-sync-mcp` as the new documented entry point; legacy `python netbox-mcp/server.py` must still work.
- [ ] **Step 4: Add the failing pipeline characterization tests.** Mock `run_sync()` and `sync_assetexplorer()` and assert: main success invokes AE second; main failure/exception skips AE; main success + AE failure is partial; unconfigured AE is skipped; `KeyboardInterrupt` is not swallowed; standalone main-only entry remains successful. Add a failing NVR test where both collection attempts and `ensure_fn()` fail, asserting no `UnboundLocalError` for `nvr_name` and the failure is recorded once in the appropriate category.
- [ ] **Step 5: Run the focused tests and confirm they fail for the intended reason.**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_pipeline.py tests\test_nvr_regressions.py tests\mcp\test_new_tools.py -q
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```
Expected: pipeline module/NVR failure are exposed; collection completes without `mcp` discovery errors when the MCP dependency is stubbed by unit tests and E2E is opted out.

- [ ] **Step 6: Commit only after focused tests pass in Task 2.** Do not commit yet; retain this task's test evidence.

---

## Task 2 — Shared pipeline, configuration isolation, reporting, and reliability fixes

**Files:**
- Create: `netbox_sync/pipeline.py`, `netbox_sync/worker.py`, `netbox_sync/locking.py`
- Modify: `netbox_sync/report.py`, `netbox_sync/netbox.py`, `netbox_sync/sync.py`, `netbox_sync/assetexplorer_sync.py`, `netbox_sync/main.py`
- Test: `tests/test_pipeline.py`, `tests/test_reporting.py`, `tests/test_locking.py`, `tests/test_worker.py`, `tests/test_scanner.py`, `tests/test_nvr_regressions.py`, `tests/test_main.py`

- [ ] **Step 1: Implement the pipeline to satisfy Task 1.** Keep `run_sync()` and `sync_assetexplorer()` behavior unchanged. `run_pipeline()` returns a structured result and uses existing stage exceptions to stop before AE. `netbox-sync` invokes the combined pipeline; `sync_all_to_netbox.py` remains main-only, and `sync_assetexplorer.py` remains AE-only. `run_daily_sync.sh` ordering remains valid.
- [ ] **Step 2: Fix `nvr_name` before any fallback use.** In `process_nvrs()`, initialize `nvr_name = _unique_nvr_name(None, probe.get('hostname'), ip, family)` before collection/ensure. On collection failure, preserve the existing camera-sweep safety behavior, and do not reference an unbound variable if ensure also fails. Do not change camera serial/channel evidence rules.
- [ ] **Step 3: Write failing durable-offline tests.** Simulate two fresh `_offline_sweep()` calls with cleared `_scan_fail_counts` and persisted `scan_misses=1`; assert threshold behavior. Assert a present device resets persisted misses, disabled families do nothing, NVR manufacturer scope is unchanged, no full custom-field replacement occurs, and state-write failure does not mark offline.
- [ ] **Step 4: Implement `scan_misses` persistence.** Add the field definition, pass the device record as the optional seventh argument, and retain compatibility for six-argument callers. Use the existing `pynetbox` device update endpoint. Reset persisted misses during each family's successful device reconciliation as well as during a sweep hit. Preserve the public fake API test contract.

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_nvr_regressions.py tests\test_scanner.py tests\test_pipeline.py -q
```
Expected: pass, including all existing scanner tests.

- [ ] **Step 5: Write failing reporting tests.** Cover thread-safe aggregation, per-run clearing, family counters, no-op updates, changed updates, creates, explicit adoptions, failures, bounded issue counts, sanitized text, and no credential substrings in snapshots. Verify all device create/update call sites use the reporting path without changing payloads or exceptions.
- [ ] **Step 6: Implement `RunReporter` and compatibility wrappers.** Preserve current console summary text and failure categories. Add `record_adoption()` calls only at existing adoption branches. Do not introduce automatic conflict repair.
- [ ] **Step 7: Write failing lock/worker tests.** Use a fake Redis client to prove atomic acquire conflict, owner-only release, TTL expiry behavior, heartbeat renewal, lost ownership, and cleanup after exceptions/interruption. Prove the worker inherits nonsecret configuration, never includes secret values in its result or arguments, streams only sanitized logs, removes its result file, maps timeout/return codes, and does not import a second engine instance into the long-lived worker process.
- [ ] **Step 8: Implement Redis lock and subprocess runner.** Use `redis.lock` only if its owner-token behavior is explicitly verified; otherwise use `SET NX PX` + compare/delete Lua and a heartbeat. Default lock TTL is 24 hours with renewal shorter than TTL. No Redis failure may silently execute a duplicate run. Add the explicit `SYNC_LOCK_BACKEND=redis` adapter to full/AE/single-device legacy workflows, including one lock around the existing daily shell pipeline, while leaving file-lock behavior as the default.

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_pipeline.py tests\test_reporting.py tests\test_locking.py tests\test_worker.py tests\test_nvr_regressions.py tests\test_scanner.py tests\test_main.py -q
.\.venv\Scripts\python.exe -m pytest tests -q --ignore=tests/plugin --ignore=tests/mcp
```
Expected: all existing engine tests and new engine tests pass.

- [ ] **Step 9: Commit the working runtime foundation.**

```bash
git add netbox_sync sync_all_to_netbox.py sync_assetexplorer.py tests docs/superpowers/specs/2026-11-18-netbox-sync-plugin-design.md
git commit -m "Add isolated sync pipeline and durable execution reporting"
```

---

## Task 3 — Installable distribution and complete MCP packaging

**Files:**
- Modify: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `netbox_sync_mcp/server.py`, `netbox_sync_mcp/netbox_api.py`, `netbox-mcp/server.py`, `netbox-mcp/netbox_api.py`, `netbox-mcp/README.md`
- Create: `netbox_sync_plugin/__init__.py`, `tests/mcp/test_transports.py`, `tests/test_packaging.py`

- [ ] **Step 1: Add failing packaging and MCP tests.** Build wheel/sdist and inspect members for all three packages, collector modules, plugin migrations/templates/static files (placeholders added by Task 4 are included when this task completes), and no `.env`, credentials, `.superpowers`, logs, or destructive utility. Assert legacy and packaged MCP modules expose identical tool names and JSON shapes for representative calls.
- [ ] **Step 2: Configure setuptools.** Use explicit package discovery for the three packages, include package data, declare Python >=3.10 and runtime dependencies from `requirements.txt` plus `mcp<2`. Do not require NetBox itself as a pip dependency. Add the two console scripts and a readable license/author metadata section without infrastructure values.
- [ ] **Step 3: Add failing MCP credential/transport tests.** Verify missing `MCP_NETBOX_TOKEN` fails safely; `NETBOX_TOKEN` alone is not used; only `requests.get` is called; non-GET is refused; exceptions do not echo authorization headers or token values; `main()` maps no args to stdio, `--sse` to `transport='sse'` with explicitly configured `MCP_MOUNT_PATH` SSE/message routes and a root ASGI mount, and `--http` to `streamable-http`; `MCP_HOST` defaults to `127.0.0.1` and `MCP_PORT` to `8901`.
- [ ] **Step 4: Implement MCP configuration and wrappers.** Preserve all existing tools and response fields. Sanitize exceptions without changing successful JSON. Read MCP environment at `main()`/client-construction time rather than raising during import, so test collection and tooling never require a token. Keep `netbox-mcp` wrapper compatibility and NanoCortex `/mcp/sse` registration boundary. Do not let the MCP package import plugin job/view modules.

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests\mcp tests\test_packaging.py -q
.\.venv\Scripts\python.exe -m build
```
Expected: MCP and artifact tests pass; no secret or excluded runtime files appear in either artifact.

- [ ] **Step 5: Defer artifact approval until Task 6.** Keep packaging/MCP tests running, but do not claim the complete artifact until migrations, templates, and static files exist and are inspected in Task 6.

- [ ] **Step 6: Commit packaging and MCP.**

```bash
git add pyproject.toml requirements.txt requirements-dev.txt netbox_sync_mcp netbox-mcp tests/mcp tests/test_packaging.py
git commit -m "Package sync engine and read-only MCP service"
```

---

## Task 4 — NetBox models, migrations, scheduling, locking, and management command

**Files:**
- Create: `netbox_sync_plugin/apps.py`, `netbox_sync_plugin/models.py`, `netbox_sync_plugin/constants.py`, `netbox_sync_plugin/services.py`, `netbox_sync_plugin/jobs.py`, `netbox_sync_plugin/migrations/__init__.py`, `netbox_sync_plugin/migrations/0001_initial.py`, `netbox_sync_plugin/management/__init__.py`, `netbox_sync_plugin/management/commands/__init__.py`, `netbox_sync_plugin/management/commands/netbox_sync.py`, `netbox_sync_plugin/templates/netbox_sync_plugin/operations.html`, `netbox_sync_plugin/static/netbox_sync_plugin/operations.css`, `netbox_sync_plugin/static/netbox_sync_plugin/operations.js`
- Modify: `netbox_sync_plugin/__init__.py`
- Test: `tests/plugin/__init__.py`, `tests/plugin/conftest.py`, `tests/plugin/test_models.py`, `tests/plugin/test_services.py`, `tests/plugin/test_jobs.py`, `tests/plugin/test_management.py`

- [ ] **Step 1: Establish a disposable NetBox 4.5.1 test environment.** Use a new database and Redis namespace and non-production devices only. Install NetBox 4.5.1 and test dependencies in the project-local isolated environment. Do not point tests at production. Document the commands in `docs/netbox-sync-plugin.md` in Task 7.
- [ ] **Step 2: Configure plugin metadata.** `PluginConfig`: name `netbox_sync_plugin`, verbose name `Netbox-sync plugin`, `base_url='netbox-sync'`, `min_version='4.5.1'`, `max_version='4.5.1'`, `queues=['sync']`, and nonsecret defaults for scheduling, retention, issue bounds, and worker timeout. `ready()` calls `super().ready()` then imports `.jobs` for system-job registration.
- [ ] **Step 3: Write failing model/service tests.** Test singleton uniqueness, all run/family/issue fields, custom `run_sync` permission, parent/child run linkage, sanitized bounded issue upsert/aggregation, retention by newest completed runs, and no secret fields. Verify job FK deletion behavior and canceled/interrupted status mapping.
- [ ] **Step 4: Implement models and initial migration.** Add only plugin-owned tables and the fixed fields/indexes. `SyncState` anchors recurring jobs; `SyncRun` stores the associated NetBox `Job`. Do not make SyncRun a public editable NetBox inventory object. Store no environment configuration or tokens.

Run in the disposable NetBox environment:
```powershell
python manage.py makemigrations netbox_sync_plugin --check --dry-run
python manage.py migrate netbox_sync_plugin
python -m pytest tests/plugin/test_models.py -q
```
Expected: migration is deterministic, applies only plugin tables, tests pass.

- [ ] **Step 5: Write failing scheduling tests.** Cover `SyncPipelineJob` name/queue, `enqueue_once(instance=state, interval=1440)`, no duplicate pending/scheduled/running jobs, manual deduplication against scheduled jobs, scheduling disabled leaves no recurring job, changed interval replaces only the stale scheduled entry, at most one catch-up job, queue/worker status, and safe error messages.
- [ ] **Step 6: Implement `SyncPipelineJob` and `SyncService`.** Register `@system_job(interval=JobIntervalChoices.INTERVAL_DAILY)` directly on `SyncPipelineJob`; NetBox will then maintain its daily recurring job on the `SyncState` instance. `SyncService.reconcile_schedule()` handles the enabled/interval settings and `manage.py netbox_sync schedule` installs/updates that job. Do not dynamically change the decorator at import time or add a second scheduler. The pipeline job runs on `queue_name='netbox_sync_plugin.sync'`; scheduled/manual requests share `SyncService.enqueue()`. Store job PK/status transitions transactionally where practical. Do not let a disabled schedule create a backlog.
- [ ] **Step 7: Write failing execution tests.** Mock the subprocess and prove: main success → AE; main failure → no AE; AE failure → partial job/history; unhandled error → failed; timeout → failed with safe summary; lock conflict → no subprocess; heartbeat loss terminates/kills the child and records failure; cancelled/interrupted run → cancelled and lock released; all final states have finished time and no credentials in job logs or issue records.
- [ ] **Step 8: Implement worker orchestration.** The worker acquires `RedisExecutionLock(get_connection('netbox_sync_plugin.sync'))`, writes queued/running/final history, invokes the fresh `netbox_sync.worker` process, forwards sanitized logs to `self.logger`, persists bounded reporter output, and raises `JobFailed` for expected stage failures. Set `job_timeout` high enough for a real full pipeline; a shorter RQ timeout would bypass graceful lock release. Do not run discovery synchronously in any web request.
- [ ] **Step 9: Add management command and shared lock tests.** `manage.py netbox_sync run` uses the same Redis lock and subprocess pipeline; `schedule` calls the idempotent scheduling service; `cleanup` enforces retention. Prove direct `sync_all_to_netbox.py` remains file-lock based, while the NetBox-aware management command and worker cannot overlap. Do not acquire both the shell `flock` and Redis recursively in one process.

Run:
```powershell
python -m pytest tests/plugin/test_services.py tests/plugin/test_jobs.py tests/plugin/test_management.py -q
python manage.py check
python manage.py netbox_sync schedule
python manage.py netbox_sync run --help
```
Expected: all job/model/service tests pass; no production data or device writes occur.

- [ ] **Step 10: Commit the plugin runtime.**

```bash
git add netbox_sync_plugin tests/plugin docs/netbox-sync-plugin.md
git commit -m "Add NetBox sync jobs scheduling and run history"
```

---

## Task 5 — Operations dashboard, health queries, and permissions

**Files:**
- Create: `netbox_sync_plugin/health.py`, `netbox_sync_plugin/views.py`, `netbox_sync_plugin/urls.py`, `netbox_sync_plugin/navigation.py`
- Modify: `netbox_sync_plugin/templates/netbox_sync_plugin/operations.html`, `netbox_sync_plugin/static/netbox_sync_plugin/operations.css`, `netbox_sync_plugin/static/netbox_sync_plugin/operations.js`
- Test: `tests/plugin/test_health.py`, `tests/plugin/test_views.py`

- [ ] **Step 1: Write failing health tests.** Build fake/ORM devices for every managed flag and one outside-scope device. Cover missing management IP by each family, primary IPv4 override, blank and placeholder serials, valid serials, duplicate valid serials, duplicate asset tags, no name/site false positives, overlapping detail checks, offline precedence, permission-restricted querysets, and unavailable/error results. Assert `offline + attention + healthy == total` and chart values equal card counts.
- [ ] **Step 2: Implement health evaluation.** Use `Device.objects.restrict(user, 'view')`, `custom_field_data`, and `_invalid_serial()`. Use `.values()`/annotation where helpful, but evaluate the fixed serial rules and overlapping conditions in Python for correctness. Build native device-list URLs with repeated `id` parameters for computed subsets; cap display links sensibly and show a count if the result is too large for a URL. Return no health details to users lacking device-view permission.
- [ ] **Step 3: Write failing view/security tests.** Cover unauthenticated redirect, view permission required, view-only user has no run control, `run_sync` required independently, GET manual action rejected, valid CSRF POST + confirmation accepted, invalid/missing confirmation rejected, duplicate active/pending run gives a safe message and no new job, selected run bounded to recent completed history, no runs/no issues/insufficient-health states, query failure shows unavailable, worker/next-schedule states, no secret strings in HTML, and navigation permissions.
- [ ] **Step 4: Implement views and URLs.** `OperationsView` uses `ContentTypePermissionRequiredMixin` with `get_required_permission() -> 'netbox_sync_plugin.view_syncrun'`. `RunSyncView` requires `netbox_sync_plugin.run_sync`, implements `post()` only, checks CSRF through normal Django middleware and an explicit confirmation field, calls `SyncService.enqueue()`, and redirects to Operations. `GET /operations/` never enqueues. Register `/plugins/netbox-sync/operations/` and `/plugins/netbox-sync/run/`; navigation points only to Operations.
- [ ] **Step 5: Implement the approved single-page presentation.** Use NetBox's base template and plugin static assets. Include overall/job/schedule state, latest success, duration, reconciled/change/issue/offline cards, selected-run family cards and completion bar, live health chart, sanitized issues, recent completed-run selector, and a permission-gated confirmed POST button. Use the softer sage-gray canvas `#dce4de`, muted surfaces/status colors, larger typography, and Material Design icons. No black canvas, pure-white custom cards, SPA, or mockup data.
- [ ] **Step 6: Implement chart without external runtime dependencies.** Use inline SVG or small local JS reading server-provided numeric data; no external CDN requirement. Expose values as accessible text/table in addition to visual bars/donut. Clearly label current health vs selected historical run and explain chart precedence/overlapping detailed checks. Do not auto-refresh into a duplicate POST.

Run:
```powershell
python -m pytest tests/plugin/test_health.py tests/plugin/test_views.py -q
python manage.py check
```
Expected: all dashboard tests pass, HTML has no credentials or unescaped raw collector responses, and GET never mutates jobs.

- [ ] **Step 7: Commit the dashboard.**

```bash
git add netbox_sync_plugin tests/plugin
git commit -m "Add permission-controlled NetBox sync operations dashboard"
```

---

## Task 6 — Full regression, packaging integration, and security verification

**Files:**
- Modify only files required by failures: existing tests remain authoritative.
- Test: `tests/plugin/test_permissions.py`, `tests/plugin/test_history.py`, `tests/test_packaging.py`

- [ ] **Step 1: Add integration tests for durable history and retention.** Cover queued/running/succeeded/partial/failed/cancelled runs, per-family data, issue aggregation/caps, interrupted subprocess cleanup, selected-run vs live-health boundaries, retention deletion order, and associated NetBox job links.
- [ ] **Step 2: Add security regression tests.** Use canary strings shaped like tokens/passwords in mocked exceptions and logs; assert they never appear in `SyncRun.summary`, `SyncIssue.message`, NetBox job error/log entries, HTML, subprocess result files, or MCP output. Assert no destructive endpoints, MCP job enqueue imports, direct ORM reconciliation writes, or non-GET MCP request methods exist.
- [ ] **Step 3: Run all hermetic tests and compile checks.**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q netbox_sync netbox_sync_plugin netbox_sync_mcp
.\.venv\Scripts\python.exe -m build
git diff --check
```
Expected: all tests pass, no discovery errors, no whitespace errors. Do not report a guessed test total; record the actual collected/passed counts.

- [ ] **Step 4: Inspect built artifacts and installation.** Install the wheel into a new project-local disposable environment with NetBox 4.5.1 test dependencies; assert import/package data/entry points/migrations resolve from the installed wheel, not the source checkout. Re-run MCP and plugin smoke tests against that installation.

- [ ] **Step 5: Commit integration/security fixes.**

```bash
git add netbox_sync netbox_sync_plugin netbox_sync_mcp tests pyproject.toml
git commit -m "Verify plugin history packaging and credential isolation"
```

---

## Task 7 — Documentation, disposable-instance acceptance, and controlled cutover

**Files:**
- Create: `docs/netbox-sync-plugin.md`
- Modify: `README.md`, `netbox-mcp/README.md`, `.gitignore` only for generated build/test artifacts if needed

- [ ] **Step 1: Document installation without real values.** Include wheel installation into NetBox's Python environment, `PLUGINS=['netbox_sync_plugin']`, plugin-owned migration, nonsecret `PLUGINS_CONFIG`, server-side sync environment, separate `MCP_NETBOX_TOKEN` (read-only `write_enabled=False`), and dedicated worker:

```bash
python manage.py rqworker netbox_sync_plugin.sync
python manage.py netbox_sync schedule
python manage.py netbox_sync run
netbox-sync-mcp --sse
```

Document the RQ timeout must accommodate a real full pipeline, Redis shared-lock requirement, lock TTL/heartbeat, and that scheduled/manual/management-command runs share history and locking. Legacy direct CLI scripts remain supported with file locks by default; for shared worker/CLI cutover set `SYNC_LOCK_BACKEND=redis` explicitly and provide the server-side Redis connection without logging it. Otherwise use the NetBox-aware command/shared Redis.
- [ ] **Step 2: Document permissions and health definitions.** Explain `view_syncrun`, `run_sync`, normal device-view restrictions, managed fields, missing-management-IP rules, serial/asset-tag checks, chart precedence, overlapping detail checks, selected run vs current health, and unavailable/empty states. Do not include real hostnames, IPs, tokens, usernames, or production counts.
- [ ] **Step 3: Document migration/cutover.** Existing cron/systemd scheduling and NetBox scheduling must not run concurrently unless both paths use the shared Redis lock. Validate the worker first, then disable the old scheduler; retain `sync_all_to_netbox.py`, `sync_assetexplorer.py`, `sync_single_device.py`, and `run_daily_sync.sh` as recovery workflows. Document their explicit `SYNC_LOCK_BACKEND=redis` mode and that file-lock-only mode is suitable only when no worker/shared-machine overlap is possible. Installation never automatically disables schedulers or changes production devices.
- [ ] **Step 4: Document MCP compatibility.** Include STDIO, `--sse` at `/mcp/sse` through the existing reverse proxy, `--http`, loopback backend defaults, and unchanged NanoCortex registration. MCP cannot enqueue jobs and does not share the write-capable sync token.
- [ ] **Step 5: Run disposable NetBox 4.5.1 acceptance.** With the operator's explicitly disposable instance and test collectors/fixtures: install/migrate only the plugin, start dedicated worker, schedule a run, enqueue a confirmed manual run, reject a duplicate, verify actual background execution, run/family/issue history, health filters/links, restricted permissions, and a read-only MCP query. Run the NetBox-aware CLI while a worker owns the lock and prove it exits without executing. Do not use production data or production collectors.
- [ ] **Step 6: Perform a visual check on the disposable instance.** Render Operations at desktop and narrow widths, compare it to the approved softer visual direction, verify chart/card values and empty/error states, and ensure no illustrative mockup values were hard-coded. Preserve screenshots only as nonsecret local test artifacts, not in Git unless explicitly approved.
- [ ] **Step 7: Run final verification and update the spec status.** Record actual test commands/results and any acceptance limitations. Mark the spec implemented only after all acceptance checks pass. Do not claim production deployment has occurred.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
git diff --check
git status --short
```

- [ ] **Step 8: Commit documentation and final verified implementation.**

```bash
git add README.md netbox-mcp/README.md docs/netbox-sync-plugin.md docs/superpowers/specs/2026-11-18-netbox-sync-plugin-design.md
git commit -m "Document NetBox sync plugin deployment and acceptance"
```

Production cutover requires separate operator approval, a backup/rollback plan, and an explicit request; it is not part of autonomous implementation.
