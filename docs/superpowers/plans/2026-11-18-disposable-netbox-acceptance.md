# Disposable NetBox 4.5.1 Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. No Git mutation is authorized.

**Goal:** Produce reproducible functional and visual acceptance evidence for the built plugin on an isolated NetBox 4.5.1 instance.

**Architecture:** A project-local Docker Compose project starts PostgreSQL, Redis, NetBox, the dedicated RQ worker, and MCP. Generated local-only settings and synthetic fixtures are ignored by Git; acceptance scripts execute checks inside the containers and store sanitized results outside source artifacts.

**Tech Stack:** Docker Compose, NetBox Community 4.5.1, PostgreSQL, Redis/RQ, Python/Django, browser acceptance tooling.

**Spec:** `docs/superpowers/specs/2026-11-18-netbox-sync-plugin-design.md` and `docs/netbox-sync-plugin.md`.

## Global Constraints

- Work in the current checkout on `main`; do not commit, push, create a worktree, or otherwise mutate Git.
- Never connect to production or use production credentials; all containers and fixtures are disposable.
- Do not modify NetBox core or replace `pynetbox` reconciliation with ORM writes.
- No destructive inventory actions, credential persistence in source/results, or production deployment.
- Fix only source defects proven by acceptance; rerun focused tests and the full suite after any fix.

## Review Focus

- A failed prerequisite must stop dependent checks and be reported rather than converted into a pass.
- Live RQ callbacks, PostgreSQL locking, subprocesses, and ownership loss require real services, not test doubles.
- Screenshots and logs must be checked for credentials and real infrastructure data before retention.
- A recurring schedule must survive repeated reconciliation without displacing immediate jobs.
- Visual acceptance requires actual browser evidence at desktop and narrow widths.

### Task 1: Build the disposable stack

**Files:** Create `acceptance/` Compose/configuration/startup files and ignore local runtime/results directories; modify `docs/netbox-sync-plugin.md` with harness commands.

- [x] Define a uniquely named Compose project, internal services, loopback-only NetBox port, NetBox 4.5.1, dedicated queue worker, generated synthetic settings, and wheel installation.
- [x] Validate Compose configuration and create/start only this project's containers and volumes.
- [x] Run NetBox migrations/checks and verify installed package imports, templates/static files, migrations, and entry points.

### Task 2: Execute functional acceptance

**Files:** Create `acceptance/` synthetic fixtures and verification scripts; store sanitized evidence under ignored `acceptance/results/`.

- [x] Exercise a confirmed manual pipeline using test collectors/fixtures; prove fresh subprocess ordering, duplicate rejection, history/issues/families, job links, and retention.
- [x] Exercise two recurrence cycles, immediate-job preservation, concurrent enqueue/reconciliation, and disabling without backlog.
- [x] Inject real post-commit RQ insertion failure and verify terminal `core.Job`/`SyncRun` state and sanitized feedback.
- [x] Exercise Redis ownership conflicts, CLI conflict behavior, ownership loss, interruption cleanup, and permissions/CSRF/device restrictions.
- [x] Exercise packaged MCP over STDIO, SSE `/mcp/sse`, and Streamable HTTP; prove GET-only requests and a real synthetic-inventory query.

### Task 3: Execute visual acceptance and report

**Files:** Create browser capture/check scripts under `acceptance/`; update acceptance results and `.superpowers/sdd/2026-11-18-netbox-sync-plugin/progress.md`.

- [x] Capture desktop and narrow Operations views for populated, empty, and error/unavailable states; inspect responsive layout, charts, and sage-gray styling.
- [x] Record each checklist item as passed, failed, or blocked with commands and sanitized evidence paths; do not claim acceptance while anything remains open.
- [x] Stop/dispose only the acceptance stack when requested by the verification flow; leave production deployment blocked pending separate approval.
