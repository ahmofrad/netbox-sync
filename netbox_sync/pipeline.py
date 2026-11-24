"""Fresh-process entry point for discovery followed by AssetExplorer."""
import os
import sys
from dataclasses import dataclass, field

from netbox_sync.assetexplorer_sync import sync_assetexplorer
from netbox_sync.config import _validate_config, log
from netbox_sync.report import sanitize_text
from netbox_sync.sync import run_sync


@dataclass
class PipelineResult:
    main_succeeded: bool = False
    assetexplorer_state: str = "skipped"
    assetexplorer_succeeded: bool | None = None
    succeeded: bool = False
    partial: bool = False
    failures: list[dict] = field(default_factory=list)
    families: dict = field(default_factory=dict)
    summary: str = ""


def run_pipeline(*, include_assetexplorer=True, reporter=None, logger=None):
    result = PipelineResult()
    logger = logger or log
    try:
        run_sync()
        result.main_succeeded = True
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result.failures.append({"stage": "discovery", "message": str(exc)})
        logger("ERROR", f"Discovery sync failed: {exc}")

    configured = bool(os.getenv("AE_URL") and os.getenv("AE_API_KEY"))
    if result.main_succeeded and include_assetexplorer and configured:
        try:
            sync_assetexplorer()
            result.assetexplorer_state = "succeeded"
            result.assetexplorer_succeeded = True
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            result.assetexplorer_state = "failed"
            result.assetexplorer_succeeded = False
            result.failures.append({"stage": "assetexplorer", "message": str(exc)})
            logger("ERROR", f"AssetExplorer sync failed: {exc}")

    result.succeeded = result.main_succeeded and (
        not include_assetexplorer
        or not configured
        or result.assetexplorer_succeeded is True
    )
    result.partial = result.main_succeeded and not result.succeeded
    if reporter is not None and not result.main_succeeded:
        reporter.finish_active_families()
    if result.failures:
        stage = result.failures[0]["stage"]
        message = sanitize_text(result.failures[0]["message"], 350)
        result.summary = sanitize_text(f"Synchronization {'partially completed' if result.partial else 'failed'}: {stage}: {message}")
    else:
        result.summary = "Synchronization completed successfully"
    if reporter is not None:
        result.families = reporter.snapshot().get("families", {})
    return result


def main(argv=None):
    import argparse
    import json
    from netbox_sync.report import get_reporter, sanitize_text

    parser = argparse.ArgumentParser(description="NetBox discovery and AssetExplorer synchronization")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", help=argparse.SUPPRESS)
    parser.add_argument("--skip-assetexplorer", action="store_true")
    args = parser.parse_args(argv)
    try:
        _validate_config()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1
    from netbox_sync.main import _acquire_lock
    if not _acquire_lock():
        log("ERROR", "Another synchronization instance is running — exiting.")
        return 1
    try:
        result = run_pipeline(include_assetexplorer=not args.skip_assetexplorer,
                              reporter=get_reporter())
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
        return 130
    if args.result_file:
        snapshot = get_reporter().snapshot()
        payload = {
            "main_succeeded": result.main_succeeded,
            "assetexplorer_state": result.assetexplorer_state,
            "assetexplorer_succeeded": result.assetexplorer_succeeded,
            "succeeded": result.succeeded,
            "partial": result.partial,
            "summary": result.summary,
            "failures": [{"stage": item["stage"],
                          "message": sanitize_text(item["message"])} for item in result.failures],
            "counters": snapshot["counters"],
            "families": snapshot["families"],
            "issues": snapshot["issues"],
        }
        with open(args.result_file, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
