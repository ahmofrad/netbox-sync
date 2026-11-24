# NetBox sync plugin deployment

The `netbox-sync-plugin` distribution contains the synchronization engine, the NetBox 4.5.1 plugin, and the read-only MCP service. Installation and scheduling do not change device inventory. Reconciliation continues to use `pynetbox`; the plugin stores only private operational history and settings, never synchronization credentials.

## Install on a disposable NetBox 4.5.1 instance

Use a new NetBox database and Redis namespace, test devices only, and a project-local build of the distribution. Install the wheel into the same Python environment as NetBox; do not add NetBox itself as a package dependency.

```bash
python -m build
python -m pip install dist/netbox_sync_plugin-1.0.0-py3-none-any.whl
```

Add the plugin to NetBox's `configuration.py`:

```python
PLUGINS = ["netbox_sync_plugin"]
PLUGINS_CONFIG = {
    "netbox_sync_plugin": {
        "schedule_enabled": False,
        "interval_minutes": 1440,
        "retention_runs": 90,
        "max_issues_per_run": 500,
        "issue_message_max_length": 500,
        "worker_timeout": 86400,
    },
}
```

These are nonsecret operational defaults. Apply only plugin-owned migrations and run NetBox's checks:

```bash
python manage.py migrate netbox_sync_plugin
python manage.py collectstatic --no-input
python manage.py check
```

Set `schedule_enabled` to `True` only when a native recurring job is desired. Its interval is expressed in minutes. These configured values initialize the `SyncState` singleton when it is first created; later database state is retained rather than overwritten on startup. The `SyncState` singleton anchors the recurring NetBox job; scheduling is disabled by default. Confirmed manual runs always use an immediate job, separate from recurrence; schedule reconciliation selects only recurring jobs and cannot cancel a pending or running manual job. Immediate enqueue and recurrence reconciliation share NetBox's scheduling advisory lock. If NetBox's post-commit RQ insertion fails, the durable run is marked failed and the UI reports a queue error. Recurring jobs retain the dedicated queue, configured timeout, and pipeline options. Disabling scheduling and running `python manage.py netbox_sync schedule` removes anchored future jobs without deleting active execution history.

Provide the existing synchronization environment only in the worker/command process environment or protected service configuration. This includes `NETBOX_URL`, `NETBOX_TOKEN`, enabled collector settings, and optional AssetExplorer settings. Do not place these values in `PLUGINS_CONFIG`, the database, command-line arguments, templates, or logs. A pipeline executes in a fresh subprocess for each run, so import-time configuration and per-process caches are not reused.

Start the dedicated worker and manage jobs through NetBox:

```bash
python manage.py rqworker netbox_sync_plugin.sync
python manage.py netbox_sync schedule
python manage.py netbox_sync run
python manage.py netbox_sync run --skip-assetexplorer
python manage.py netbox_sync run --lock-timeout 0  # fail immediately on conflict; positive values wait up to N seconds
python manage.py netbox_sync cleanup
```

The RQ worker timeout and plugin `worker_timeout` must accommodate a complete discovery-plus-AssetExplorer run. A shorter RQ timeout can interrupt graceful cleanup. The default shared lock TTL is 24 hours, with owner-checked heartbeat renewal and release. While a run is active, the plugin parent also persists `SyncState.last_heartbeat`; the dashboard uses that timestamp as worker-health evidence, not as proof that a selected historical run succeeded. Crash recovery relies on TTL expiry; a stale lock is never simply deleted. Timeout or ownership loss signals the fresh subprocess's dedicated process group, waits briefly, and then kills it if necessary.

Scheduled, confirmed-manual, and management-command runs use the same NetBox Redis connection, execution lock, and run history. A lock conflict fails closed without starting a subprocess. Main synchronization must succeed before AssetExplorer starts. Each fresh subprocess receives inherited server-side environment and a nonsecret run identifier; result files are owner-restricted, sanitized, temporary, and removed in `finally`. A valid result file is persisted even when the subprocess exits nonzero, so partial discovery/AssetExplorer history is not discarded.

After result and issue persistence, the configured `retention_runs` cap is applied to completed runs only. Active runs are never removed by automatic retention. Each history row records plugin version `1.0.0` and, when explicitly supplied by the deployment, the nonsecret source revision in `NETBOX_SYNC_SOURCE_REVISION`. If that variable is absent, the revision remains blank; runtime code does not inspect the source checkout or Git repository.

## Operations permissions and health

Open **Netbox-sync Operations** under the plugin navigation. The URLs are:

- `/plugins/netbox-sync/operations/`
- `/plugins/netbox-sync/run/` (POST only)

Grant permissions independently:

- `netbox_sync_plugin.view_syncrun`: view Operations and private run history.
- `netbox_sync_plugin.run_syncrun`: enqueue a confirmed synchronization.

Starting a run also requires CSRF validation and the explicit confirmation field. GET requests never enqueue work. A queued or running pipeline prevents duplicate enqueueing. Device health is additionally restricted by normal `dcim.view_device` permission and `Device.objects.restrict(user, "view")`; users without it receive no health details.

The dashboard deliberately separates the selected completed run from current NetBox inventory health. A selected run supplies counters, family results, issues, duration, and AssetExplorer status. Current health evaluates managed devices at page-render time and is independent of that history.

A device is managed when any existing `custom_field_data` flag is true: `redfish_enabled`, `storage_enabled`, `san_switch_enabled`, `cisco_enabled`, `fortigate_enabled`, `fortiweb_enabled`, `ftd_enabled`, `wlc_enabled`, `unifi_enabled`, `nvr_enabled`, `wap_enabled`, or `cam_enabled`.

Health checks cover:

- **Offline:** NetBox device status is `offline`.
- **Missing management IP:** no primary IPv4 and no nonblank applicable management field. Management fields are `bmc_ip`, `storage_ip`, `san_switch_ip`, `cisco_ip`, `fortigate_ip`, `fortiweb_ip`, `ftd_ip`, `wlc_ip`, `unifi_ip`, `nvr_ip`, and `cam_ip`. `wap_enabled` uses primary IPv4 only.
- **Missing or invalid serial:** the synchronization engine's normal placeholder/blank serial rules apply.
- **Duplicate valid serial:** valid serials are compared within managed inventory.
- **Duplicate nonblank asset tag:** legacy duplicate data is reported even though normal NetBox validation prevents new duplicates.

Chart precedence is **offline → attention → healthy**. Detailed checks may overlap, but each device appears in exactly one chart category. Detail links contain device IDs only, never serials or asset tags, and are capped at 100 IDs. Empty inventory, unavailable health, unavailable worker heartbeat, and absent run history are distinct states; a health query failure is not displayed as zero healthy devices. The schedule panel shows the next anchored run when available. Queued work is not described as an active worker, and heartbeats older than ten minutes are explicitly marked stale.

The page uses local CSS and JavaScript and an accessible SVG chart. It performs no discovery, MCP query, automatic refresh, or background network request in the web process.

## Legacy CLI compatibility and cutover

The existing workflows remain available:

```bash
python sync_all_to_netbox.py
python sync_assetexplorer.py
python sync_single_device.py
./run_daily_sync.sh
netbox-sync
```

The direct legacy scripts retain their local file-lock behavior by default. Set `NETBOX_SYNC_LOCK_FILE` to a writable path when the installed package directory is read-only; if unset, the original package-adjacent lock path remains unchanged. File locks alone are suitable only when no worker or cross-process shared-lock overlap is possible.

For a controlled shared-worker cutover, explicitly set `SYNC_LOCK_BACKEND=redis` and provide `SYNC_REDIS_URL` in the protected server-side environment. Legacy full-sync, AssetExplorer, single-device, and ordered daily workflows then use the shared Redis ownership lock and fail closed when Redis is unavailable. The daily Redis wrapper holds one outer lock around the ordered pipeline; child scripts use file mode to avoid recursive Redis acquisition. Never log the Redis URL.

Do not run cron/systemd scheduling concurrently with NetBox scheduling unless both paths use the shared Redis lock. Validate the NetBox worker on the disposable instance first, then disable the old scheduler as a separate operator-controlled step. Keep the legacy commands as recovery workflows. Installation never disables an existing scheduler, wipes inventory, repairs collisions, or changes production devices automatically.

Production cutover requires separate approval, a backup, and a rollback plan. No production deployment is implied by installing or testing this distribution.

## Read-only MCP service

MCP uses `NETBOX_URL` plus a **separate** `MCP_NETBOX_TOKEN`. It never inherits the synchronization `NETBOX_TOKEN`. Create the MCP token with NetBox `write_enabled=False` and least-privilege object read permissions. `NETBOX_VERIFY_TLS` controls TLS verification and should remain enabled with a trusted certificate in production.

Run one transport:

```bash
netbox-sync-mcp                 # STDIO
netbox-sync-mcp --sse           # SSE
netbox-sync-mcp --http          # Streamable HTTP
```

Backend defaults are `MCP_HOST=127.0.0.1`, `MCP_PORT=8901`, and `MCP_MOUNT_PATH=/mcp`. Preserve the existing reverse proxy so SSE is publicly registered at `/mcp/sse`; do not expose the loopback backend directly. NanoCortex registration remains a remote MCP named `netbox` pointing to the existing TLS endpoint `/mcp/sse`.

MCP tools issue HTTP GET only. They cannot enqueue jobs, run discovery, modify inventory, or access plugin execution services. Keep request errors and credentials server-side; do not expose token values in service command lines, proxy logs, documentation, or NanoCortex configuration.

The legacy launcher remains available as `python netbox-mcp/server.py` and uses the same packaged implementation and `MCP_NETBOX_TOKEN` boundary.

## Disposable acceptance checklist

Before considering production cutover, complete all of the following against NetBox 4.5.1 with test collectors/fixtures only:

1. Install the wheel outside the source checkout; check imports, entry points, static files, templates, and deterministic plugin migration.
2. Start the dedicated worker; enable and schedule a run; confirm exactly one recurring job and no backlog when disabled. Repeat reconciliation while immediate manual jobs are pending and running; prove both survive. Run concurrent manual enqueue/schedule operations and verify PostgreSQL advisory-lock serialization and one active run.
3. Queue a confirmed manual run, reject a duplicate, and verify actual fresh-process discovery followed by AssetExplorer only after main success. Inject an RQ enqueue failure after commit; verify the real `core.Job`/`SyncRun` terminal state, sanitized error feedback, and no queue execution.
4. Verify run/family/issue history, sanitized errors, retention, lock ownership loss, interruption cleanup, and job links.
5. While the worker owns the lock, run `manage.py netbox_sync run` and the explicitly Redis-enabled legacy path; prove neither executes discovery.
6. Verify view-only/run permissions, device-restricted health filters and links, CSRF/confirmation rejection, and a GET-only MCP query.
7. Render Operations at desktop and narrow widths; compare against the approved sage-gray direction and check real/empty/error states. Preserve nonsecret screenshots only as local test artifacts unless separate approval is given.

Record the actual acceptance results. Do not mark the implementation fully accepted or deploy to production while any item remains unverified.

### Recorded result — 2026-11-24

All seven checks above passed against the disposable NetBox 4.5.1 stack with synthetic fixtures. This includes real RQ scheduling/execution and failure paths, PostgreSQL enqueue/reconciliation serialization, Redis ownership and interruption behavior, live permissions, and all three MCP transports. STDIO, SSE `/mcp/sse`, and Streamable HTTP `/mcp/` returned the synthetic inventory through three observed outbound GET requests; the separate database token had `write_enabled=False`, non-GET methods were refused, and the token was not returned.

Seven Operations scenarios passed at 1440 px and 390 px: populated, failed, empty/error, unavailable health, queued, running, and stale heartbeat. All 14 captures had no horizontal overflow or browser errors, used the local sage-gray stylesheet, and matched their state-specific banners and chart assertions. The 14 screenshots were visually inspected; transient response JSON was removed after capture. Generated credentials were absent from retained text artifacts and the built wheel/sdist.

The full suite passed with **516 tests passed, 1 skipped**. `compileall`, wheel/sdist construction, required package-data checks, and `git diff --check` passed. Sanitized local evidence is under Git-ignored `acceptance/results/`; its PowerShell UTF-16 `visual.json` log and build logs are not machine-readable acceptance summaries. Production deployment remains blocked pending separate approval, a backup, and a rollback plan.
