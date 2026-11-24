"""Built-distribution contents and isolated installed-artifact smoke tests."""
import csv
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import venv
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    output = tmp_path_factory.mktemp("dist")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output), str(ROOT)],
        cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=180,
    )
    wheel = next(output.glob("*.whl"))
    sdist = next(output.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    return wheel, sdist, wheel_names, sdist_names


def test_artifacts_include_all_runtime_packages_and_plugin_data(artifacts):
    _, _, wheel, sdist = artifacts
    required = {
        "netbox_sync/__init__.py",
        "netbox_sync/pipeline.py",
        "netbox_sync/worker.py",
        "netbox_sync/collectors/assetexplorer.py",
        "netbox_sync_plugin/__init__.py",
        "netbox_sync_plugin/migrations/0001_initial.py",
        "netbox_sync_plugin/templates/netbox_sync_plugin/operations.html",
        "netbox_sync_plugin/static/netbox_sync_plugin/operations.css",
        "netbox_sync_plugin/static/netbox_sync_plugin/operations.js",
        "netbox_sync_mcp/server.py",
        "netbox_sync_mcp/netbox_api.py",
    }
    assert required <= wheel
    assert required <= sdist
    for names in (wheel, sdist):
        assert not any(
            name.startswith(("tests/", ".superpowers/", "netbox-mcp/"))
            or "/__pycache__/" in name
            or name.endswith((".env", ".pyc", ".log", "netbox-sync.lock"))
            or "/.env" in name
            for name in names
        )


def test_wheel_declares_only_intended_console_scripts(artifacts):
    wheel_path, _, names, _ = artifacts
    entry_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
    with zipfile.ZipFile(wheel_path) as archive:
        entries = archive.read(entry_name).decode("utf-8")
    assert "netbox-sync = netbox_sync.pipeline:main" in entries
    assert "netbox-sync-mcp = netbox_sync_mcp.server:main" in entries


def test_wheel_imports_and_resolves_plugin_data_outside_source_checkout(artifacts, tmp_path):
    wheel_path, _, _, _ = artifacts
    target = tmp_path / "installed"
    environment = dict(os.environ, PYTHONPATH="", PYTHONNOUSERSITE="1")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel_path)],
        cwd=tmp_path, env=environment, check=True, capture_output=True, text=True, timeout=120,
    )
    script = """
import importlib
from importlib.metadata import distribution
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
for name in (
    'netbox_sync', 'netbox_sync.pipeline', 'netbox_sync.collectors.assetexplorer',
    'netbox_sync_mcp', 'netbox_sync_mcp.netbox_api',
):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
import django
from django.conf import settings
settings.configure(
    SECRET_KEY='artifact-test-only', INSTALLED_APPS=[
        'django.contrib.auth', 'django.contrib.contenttypes', 'netbox_sync_plugin',
    ],
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
    DEFAULT_AUTO_FIELD='django.db.models.BigAutoField', NETBOX_SYNC_TESTING=True,
)
django.setup()
from netbox_sync_plugin.models import SyncRun
from netbox_sync_plugin import navigation
assert SyncRun._meta.get_field('job').remote_field.model == 'core.Job'
assert len(navigation.menu_items) == 1
for relative in (
    'migrations/0001_initial.py',
    'templates/netbox_sync_plugin/operations.html',
    'static/netbox_sync_plugin/operations.css',
    'static/netbox_sync_plugin/operations.js',
):
    assert (Path(sys.argv[1]) / 'netbox_sync_plugin' / relative).is_file()
metadata = distribution('netbox-sync-plugin')
assert metadata.version == '1.0.0'
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(target)], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
