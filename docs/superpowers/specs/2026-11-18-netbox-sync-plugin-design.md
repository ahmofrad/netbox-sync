# Netbox-sync plugin and Operations dashboard — design spec

**Date:** 2026-11-18
**Status:** Hermetic implementation and disposable NetBox 4.5.1 functional/visual acceptance verified; production deployment blocked pending separate approval
**Target NetBox:** Community 4.5.1

## 1. Purpose

Deliver one installable, self-contained **Netbox-sync plugin** distribution that packages the existing synchronization engine, the read-only NetBox MCP service, NetBox-managed scheduled/manual synchronization, durable operations reporting, and a single **Operations** dashboard.

NetBox core source files must not be patched. Existing CLI workflows remain supported for recovery, testing, migration, and standalone use. Existing synchronization identity, adoption, HA/stack, offline, collision, inventory, and cabling semantics must be preserved.

## 2. Approved scope

### Included

- All existing device collectors, discovery, reconciliation, IPAM, power, cabling, hardware inventory, AssetExplorer, and single-device synchronization code.
- NetBox `JobRunner` execution on a plugin-owned worker queue.
- Existing daily scheduling behavior, with AssetExplorer following a successful main synchronization.
- Permission-controlled manual synchronization from the dashboard.
- Durable run, per-family, and sanitized issue reporting.
- Current NetBox inventory-health summaries and links to filtered device lists.
- Existing read-only MCP tools, including STDIO, SSE, and Streamable HTTP launch modes.
- Existing CLI entry points and shared synchronization code.
- Focused fixes for the confirmed NVR failure-path and nonpersistent offline-counter defects, covered by regression tests.

### Excluded from the first release

- NetBox core modifications or monkey patches.
- Rewriting proven `pynetbox` reconciliation to use Django ORM directly.
- A separate frontend application, frontend framework, or external sync-control service.
- Additional dashboard pages, a configuration editor, or credential forms.
- UI device deletion, wipe, manual-offline, or conflict-repair actions.
- A new single-device UI; the existing single-device CLI remains available.
- Exposing MCP write tools or allowing MCP to enqueue synchronization.
- Automatic rollback of partially completed synchronization runs.
- Direct production installation without prior disposable-instance acceptance testing.

The separately managed destructive wipe utility is not included in the installable runtime distribution or exposed by the plugin.

## 3. Architecture

### 3.1 Distribution and package boundaries

One Python distribution named `netbox-sync-plugin` contains:

- `netbox_sync`: existing collectors, synchronization, reconciliation, configuration, and reporting, with narrowly scoped adapters for runtime configuration, logging, counters, and run history.
- `netbox_sync_plugin`: NetBox `PluginConfig`, models, migrations, jobs, views, navigation, templates, static assets, filters/health queries, and permissions.
- `netbox_sync_mcp`: existing read-only MCP tools and GET-only NetBox client.

The plugin's display name is **Netbox-sync plugin**. Its canonical base URL is `/plugins/netbox-sync/`; the Operations page is `/plugins/netbox-sync/operations/`.

The package declares its runtime dependencies and includes migrations, templates, static assets, and MCP entry points in built artifacts. Existing command-line scripts remain thin entry points into the shared engine.

### 3.2 Execution topology

NetBox web processes serve the dashboard and enqueue jobs but never perform discovery in request/response cycles. NetBox workers execute the plugin's `JobRunner` on a dedicated queue. The worker has the same server-side environment configuration as the CLI.

The MCP server ships in the same distribution but runs as a separate service, as it does today. It is not embedded in the web or worker process. Its current STDIO behavior and reverse-proxy-compatible `/mcp/sse` route remain supported, as does Streamable HTTP.

All write-capable reconciliation continues through the existing NetBox API abstraction. The plugin does not bypass API validation by writing directly to NetBox models.

## 4. Execution and scheduling

### 4.1 Job behavior

A combined pipeline job runs:

1. Main discovery and synchronization.
2. AssetExplorer synchronization only if the main synchronization succeeds.

This preserves `run_daily_sync.sh` ordering. Each stage receives durable history and per-family results. The pipeline fails according to existing CLI outcome semantics; a partial run does not imply rollback.

Recurring scheduling uses the documented NetBox system-job/job-runner facilities supported by NetBox 4.5.1. The default interval preserves the current daily operational schedule. Plugin configuration controls whether scheduling is enabled and its interval, without exposing secrets.

A manual **Run synchronization** action enqueues the same pipeline. It is not a second synchronization implementation.

### 4.2 Duplicate and overlap prevention

Two independent safeguards apply:

- Enqueue-time deduplication avoids repeated queued jobs for the same pipeline.
- Execution-time mutual exclusion prevents simultaneous execution from workers or the CLI.

The execution guard must support shared worker/CLI deployment rather than relying on process-local memory. Its concrete backend will be selected in the implementation plan based on the target deployment's shared database/Redis/filesystem facilities. Lock ownership, stale-lock recovery, and release in normal/error/interrupted paths must be tested.

When another run is pending or active, a manual request reports that condition and does not enqueue or execute a duplicate. Missed recurring intervals create at most one pending catch-up run, not a backlog storm.

Closing or refreshing the browser does not cancel a running job.

### 4.3 Configuration and isolation

Existing environment-based settings remain compatible. Runtime configuration is resolved at execution boundaries rather than captured solely by import-time constants, allowing worker and CLI calls to use isolated, testable run contexts.

Per-run NetBox client caches and reporting state must not leak between jobs. The plugin does not place secrets in Django model fields, templates, logs, or API responses.

The sync credential remains write-capable as required by reconciliation. The MCP credential remains separately configured and read-only.

## 5. Durable operations data

### 5.1 Models

`SyncRun` represents a full pipeline or an individual synchronization stage. It records:

- Run/stage type and parent pipeline where applicable.
- Trigger source and requesting user, when applicable.
- Associated NetBox job reference.
- Queued, started, and finished timestamps and duration.
- Status: queued, running, succeeded, partial success, failed, or cancelled.
- Plugin version and configured source revision, if available.
- Aggregate discovered, created, updated, adopted, unchanged, failed, and offline counts.
- AssetExplorer execution/skipped state where relevant.

`SyncFamilyResult` belongs to a run and records the family key, display label, outcome, timing, discovered count, change/adoption/offline counts, and failure count.

`SyncIssue` belongs to a run, optionally to a family result, and records category, severity, bounded device/source context, an optional NetBox object link, a sanitized message, and occurrence count. Secrets, credential-bearing URLs, raw authentication responses, and unbounded raw responses must not be stored.

Run history is private operational data, not a general-purpose NetBox object type. Retention limits and scheduled cleanup are configurable and bounded. NetBox's existing job logs remain available for detailed troubleshooting; the dashboard does not bulk-load those logs.

### 5.2 Reporting instrumentation

Small adapters extend the existing failure report and reconciliation paths with structured counters and issues. Instrumentation must not silently alter success/failure semantics or swallow exceptions.

The same instrumentation supports CLI and worker execution. A CLI run can persist history when plugin storage is available; otherwise its existing console behavior remains unchanged.

Before relying on worker execution, the confirmed defects below receive regression coverage and fixes:

- NVR collection failure must not reference an undefined or stale `nvr_name`.
- Consecutive offline misses must persist across process executions rather than only in `_scan_fail_counts` process memory.
- NVR-related helper imports must match the current code paths; if HDD collection is removed by the newer ManageEngine-only implementation, obsolete failing paths are not reintroduced.

No unrelated reconciliation refactoring is included.

## 6. Operations dashboard

### 6.1 Page structure

A single **Netbox-sync → Operations** page presents:

1. Overall synchronization state, worker/scheduling state, latest successful completion, duration, and next scheduled run.
2. Devices reconciled, changes applied, issues requiring attention, and current offline-device count.
3. Latest or selected run's per-family status, counts, and completion visualization.
4. Current inventory health with a chart and explicit health checks.
5. Sanitized failures and identity issues with relevant NetBox links.
6. A permission-controlled manual-run action and recent-run selector.

The implementation uses NetBox templates, navigation, icon conventions, reusable components, and small plugin-owned static assets. It does not add a separate SPA or API frontend.

### 6.2 Approved visual direction

The approved visual reference is `.superpowers/brainstorm/475-1794986208/content/operations-dashboard-soft-v2.html`, retained locally and excluded from Git.

The visual direction is a slightly deepened sage-gray canvas (`#dce4de`), complementary muted card surfaces, larger readable typography, softened status colors, icons, a run-completion bar, and an inventory-health chart. No black page background or pure-white card surfaces are used in the custom dashboard theme.

The visual mockup's names, counts, timestamps, and device identifiers are illustrative. They are not production data and must not be treated as expected live values.

### 6.3 Selected-run and live-data boundaries

The page defaults to the latest completed run and permits selection from a bounded recent-run list. Per-family results and historical issues reflect the selected run. Inventory health always reflects the current NetBox state and is labeled accordingly.

A collector/query failure produces a visible **inventory health unavailable** state, not a misleading zero. Empty states distinguish no runs, no issues, and insufficient data. Percentages and chart values derive from the same underlying counts as the cards.

## 7. Current inventory-health definitions

The managed-device scope is explicit and based on existing synchronization-enabled family fields/roles. A device is not labeled healthy merely because it appeared in a successful run.

### Offline managed devices

Managed devices whose current NetBox status is offline. This summary links to the filtered NetBox device list.

### Missing management IP

Managed devices with neither a primary IPv4 address nor the relevant family management-IP custom field. The family-specific field map is part of implementation and tests.

### Missing hardware identity

Managed devices whose serial is blank or invalid under the existing `_invalid_serial` rules. Placeholder serials are not treated as real hardware identity.

### Identity conflicts

- Nonblank, valid serials shared by multiple devices.
- Nonblank asset tags shared by multiple devices.
- Name/site/role collisions only when the existing synchronization rules treat them as unresolved identity conflicts.

NetBox global uniqueness constraints are respected; the health query must not invent constraints stricter than current reconciliation behavior.

### Chart accounting

The chart covers **accounted managed devices**, excluding devices outside the plugin-managed scope. Categories are mutually exclusive using this precedence:

1. Offline.
2. Attention-required: at least one enabled health check fails.
3. Healthy: in managed scope with no enabled health-check failure.

Each category's count is reproducible by a documented filter. A card may represent overlapping detailed checks, but the chart uses the precedence above. The UI explains this distinction.

## 8. MCP integration

All existing tool names and response behavior remain supported. The GET-only client retains its non-GET guard, and the MCP service cannot call the write-capable reconciliation functions.

Supported launch modes:

- STDIO for local clients.
- SSE at the existing reverse-proxy-compatible `/mcp/sse` route.
- Streamable HTTP for compatible clients.

The packaged service keeps the existing NanoCortex registration boundary. Documentation describes the new package entry point and any compatibility wrapper without requiring changes to tool consumers.

MCP uses a separately configured read-only NetBox token with `write_enabled=False` and appropriate object permissions. Its token never appears in tool output or logs. The dashboard does not proxy or expose MCP credentials.

## 9. Security and permissions

- Dashboard access requires a plugin-specific view permission and NetBox authentication.
- Manual execution requires a separate `run_sync` permission.
- The manual action is POST-only, CSRF-protected, confirmed by the user, and reauthorized on the server.
- Navigation visibility follows permissions but is not the security boundary.
- Current-device links respect ordinary NetBox object-view permissions.
- No environment dump, credential editor, token display, or secret-bearing raw response is available in the UI.
- Logs and persisted issue messages are sanitized before storage.
- MCP access remains read-only and separate from dashboard/job permissions.
- The first release provides no destructive action or automatic conflict repair.

## 10. Packaging, deployment, and migration

Deployment documentation covers:

1. Installing the distribution into NetBox's Python environment.
2. Enabling `netbox_sync_plugin` through normal NetBox plugin configuration.
3. Applying only plugin-owned database migrations.
4. Supplying server-side sync and MCP environment settings.
5. Running a worker for the plugin-owned queue.
6. Retaining/configuring the MCP service and reverse-proxy route.
7. Checking scheduling, dashboard permissions, and read-only MCP operation.

Existing cron/systemd scheduling and the new NetBox scheduler must not run concurrently without the shared execution guard. Migration documentation provides a controlled cutover: validate worker execution, then disable the old scheduler, while retaining CLI entry points for manual recovery.

No automatic production data migration or scheduler change is performed by installation alone.

## 11. Testing and acceptance

### Unit and regression tests

- Existing synchronization tests remain green.
- NVR failure-path, offline persistence, and current-import regression tests cover the focused fixes.
- Existing identity, adoption, HA/stack, collision, offline, inventory, and AssetExplorer tests remain authoritative.
- Runtime configuration, cache isolation, logging, and reporting adapters have CLI/worker parity tests.

### Job and persistence tests

- Scheduling and manual enqueue behavior.
- Duplicate queue prevention and cross-entry-point overlap protection.
- Main-sync/AssetExplorer ordering and failure propagation.
- Successful, partial, failed, cancelled, and interrupted-run history.
- Sanitized issue persistence and retention cleanup.

### Dashboard tests

- View/run permissions, CSRF-protected POST, and confirmation behavior.
- Accurate selected-run and aggregate counts.
- Chart/card consistency and documented precedence.
- Every health-check filter and managed-scope boundary.
- Empty states, query failures, and unavailable-health behavior.
- No secrets in responses or persisted issues.

### MCP and packaging tests

- Existing MCP tool behavior and response shapes.
- GET-only request enforcement and credential separation.
- STDIO, SSE, and Streamable HTTP startup.
- Built-distribution contents: engine, plugin, migrations, templates, static files, and MCP service.

### Disposable-instance acceptance

Installation and execution must first pass on a disposable NetBox 4.5.1 instance:

1. Install and migrate only the plugin.
2. Start the plugin worker and verify a scheduled and manual background run.
3. Verify run/family/issue history and current inventory-health queries.
4. Verify permissions and duplicate-run rejection.
5. Execute a real read-only MCP query through the packaged service.
6. Confirm existing CLI execution remains functional and mutually exclusive with the worker.

Production cutover occurs only after these checks and an operator-approved backup/rollback plan. No NetBox production data is changed during design or unapproved test setup.

## 12. Success criteria

The release is successful when one installable plugin distribution provides all agreed sync and MCP runtime code, NetBox-managed scheduling/manual execution, the supported CLI fallback, and a permission-controlled Operations dashboard with accurate historical and current-health reporting. NetBox core remains untouched, secrets remain server-side, and existing synchronization behavior is preserved except for the explicitly tested reliability fixes.
