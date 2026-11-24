"""Acceptance-only fixture shim; never installed in the distribution."""
import os
import signal
import subprocess
import time

method_log = os.getenv("NETBOX_SYNC_ACCEPTANCE_MCP_METHOD_LOG")
if method_log:
    from requests.sessions import Session
    real_request = Session.request

    def observed_request(self, method, url, *args, **kwargs):
        with open(method_log, "a", encoding="utf-8") as marker:
            marker.write(f"{method.upper()}\n")
        return real_request(self, method, url, *args, **kwargs)

    Session.request = observed_request

if os.getenv("NETBOX_SYNC_ACCEPTANCE_FIXTURES") == "1":
    def fixture_pipeline(*, include_assetexplorer=True, reporter=None, logger=None):
        from netbox_sync.pipeline import PipelineResult
        result = PipelineResult(main_succeeded=True, succeeded=True)
        child = None
        if os.getenv("NETBOX_SYNC_ACCEPTANCE_TREE") == "1":
            child = subprocess.Popen([
                "/opt/netbox/venv/bin/python", "-c",
                "import os,time; open('/tmp/netbox-sync-acceptance-grandchild.marker','w').write(str(os.getpid())); time.sleep(120)",
            ], start_new_session=False)
            signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
        result.assetexplorer_state = "succeeded" if include_assetexplorer else "skipped"
        result.assetexplorer_succeeded = True if include_assetexplorer else None
        result.summary = "Synthetic acceptance pipeline completed successfully"
        with open("/tmp/netbox-sync-acceptance-child.marker", "w", encoding="utf-8") as marker:
            marker.write(str(os.getpid()))
        try:
            time.sleep(float(os.getenv("NETBOX_SYNC_ACCEPTANCE_DELAY", "0")))
        except KeyboardInterrupt:
            if child is not None:
                child.wait(timeout=5)
            raise
        with open("/tmp/netbox-sync-acceptance-main.marker", "a", encoding="utf-8") as marker:
            marker.write(f"{os.getpid()}\n")
        if include_assetexplorer:
            with open("/tmp/netbox-sync-acceptance-assetexplorer.marker", "a", encoding="utf-8") as marker:
                marker.write("after-main\n")
        if reporter is not None:
            reporter.start_family("fixture", "Synthetic fixture")
            reporter.record_device_outcome("unchanged", family="fixture")
            reporter.finish_family("fixture")
            result.families = reporter.snapshot().get("families", {})
        return result

    # Acceptance launchers import this patch before calling the real main().
    # run_subprocess() remains unchanged; its Python argv is wrapped below.
    import subprocess
    class FixturePopen(subprocess.Popen):
        def __init__(self, command, *args, **kwargs):
            if (isinstance(command, list) and len(command) >= 4
                    and command[1:4] == ["-m", "netbox_sync.pipeline", "--worker"]):
                expression = ("import sitecustomize as fixture, netbox_sync.pipeline as p; "
                              "p.run_pipeline = fixture.fixture_pipeline; "
                              "raise SystemExit(p.main())")
                command = [command[0], "-c", expression, *command[4:]]
            super().__init__(command, *args, **kwargs)

    subprocess.Popen = FixturePopen
