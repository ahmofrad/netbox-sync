from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_disposable_harness_pins_netbox_and_only_publishes_loopback():
    compose = (ROOT / "acceptance" / "compose.yaml").read_text()
    assert "netbox-sync-acceptance:v4.5.1" in compose
    assert "FROM netboxcommunity/netbox:v4.5.1" in (ROOT / "acceptance" / "Dockerfile").read_text()
    assert '"rqworker", "netbox_sync_plugin.sync"' in compose
    assert "127.0.0.1:18080:8080" in compose
    assert "5432:5432" not in compose
    assert "6379:6379" not in compose


def test_acceptance_runtime_and_results_are_ignored():
    ignore = (ROOT / ".gitignore").read_text()
    assert "acceptance/runtime/" in ignore
    assert "acceptance/results/" in ignore


def test_acceptance_credentials_are_generated_and_loaded_from_ignored_runtime():
    harness = "\n".join(path.read_text() for path in (ROOT / "acceptance").rglob("*.py"))
    fixed_secrets = (
        "acceptance-only-" + suffix for suffix in ("db-password", "admin-password")
    )
    assert not any(value in harness for value in fixed_secrets)
    assert "sync" + "fixture1" not in harness
    assert "mcp" + "fixture12" not in harness
    assert "fixture-" + "password" not in harness
    bootstrap = (ROOT / "acceptance" / "checks" / "bootstrap.py").read_text()
    assert 'token=os.environ["NETBOX_TOKEN"]' in bootstrap
    assert 'token=os.environ["MCP_NETBOX_TOKEN"]' in bootstrap
    assert 'set_password(credentials["password"])' in bootstrap
    visual = (ROOT / "acceptance" / "checks" / "visual_responses.py").read_text()
    assert 'password=credentials["password"]' in visual
