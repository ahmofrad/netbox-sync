"""Combined discovery/AssetExplorer pipeline regression tests."""

import pytest

from netbox_sync import pipeline


@pytest.fixture(autouse=True)
def _configured_assetexplorer(monkeypatch):
    monkeypatch.setenv("AE_URL", "https://assetexplorer.test")
    monkeypatch.setenv("AE_API_KEY", "unit-test-placeholder")


def test_main_success_runs_assetexplorer_second(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_sync", lambda: calls.append("main"))
    monkeypatch.setattr(pipeline, "sync_assetexplorer", lambda: calls.append("assetexplorer"))

    result = pipeline.run_pipeline()

    assert calls == ["main", "assetexplorer"]
    assert result.main_succeeded is True
    assert result.assetexplorer_state == "succeeded"
    assert result.assetexplorer_succeeded is True
    assert result.succeeded is True
    assert result.partial is False


def test_main_exception_finalizes_active_families_and_reports_failure(monkeypatch):
    from netbox_sync import report
    reporter = report.RunReporter()
    reporter.start_family("servers")
    monkeypatch.setattr(pipeline, "run_sync",
                        lambda: (_ for _ in ()).throw(RuntimeError("main failed token=fake-pipeline-canary")))
    result = pipeline.run_pipeline(reporter=reporter)
    assert result.families["servers"]["status"] == "failed"
    assert "Synchronization failed" in result.summary
    assert "main failed" in result.summary
    assert "fake-pipeline-canary" not in result.summary


def test_main_exception_skips_assetexplorer(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_sync", lambda: (_ for _ in ()).throw(RuntimeError("main failed")))
    monkeypatch.setattr(pipeline, "sync_assetexplorer", lambda: calls.append("assetexplorer"))

    result = pipeline.run_pipeline()

    assert calls == []
    assert result.main_succeeded is False
    assert result.assetexplorer_state == "skipped"
    assert result.assetexplorer_succeeded is None
    assert result.succeeded is False
    assert result.partial is False
    assert result.failures


def test_assetexplorer_failure_is_partial(monkeypatch):
    monkeypatch.setattr(pipeline, "run_sync", lambda: None)
    monkeypatch.setattr(
        pipeline,
        "sync_assetexplorer",
        lambda: (_ for _ in ()).throw(RuntimeError("assetexplorer failed")),
    )

    result = pipeline.run_pipeline()

    assert result.main_succeeded is True
    assert result.assetexplorer_state == "failed"
    assert result.assetexplorer_succeeded is False
    assert result.succeeded is False
    assert result.partial is True
    assert len(result.failures) == 1


def test_unconfigured_assetexplorer_is_skipped_after_main(monkeypatch):
    monkeypatch.delenv("AE_URL")
    monkeypatch.delenv("AE_API_KEY")
    calls = []
    monkeypatch.setattr(pipeline, "run_sync", lambda: calls.append("main"))
    monkeypatch.setattr(pipeline, "sync_assetexplorer", lambda: calls.append("assetexplorer"))

    result = pipeline.run_pipeline()

    assert calls == ["main"]
    assert result.main_succeeded is True
    assert result.assetexplorer_state == "skipped"
    assert result.assetexplorer_succeeded is None
    assert result.succeeded is True
    assert result.partial is False


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(pipeline, "run_sync", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_pipeline()


def test_main_only_legacy_entry_remains_successful(monkeypatch):
    from netbox_sync import main as main_module

    monkeypatch.setattr(main_module, "_validate_config", lambda: None)
    monkeypatch.setattr(main_module, "_acquire_lock", lambda: True)
    monkeypatch.setattr(main_module, "run_sync", lambda: None)

    assert main_module.main() == 0
