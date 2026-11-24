"""Minimal isolated Django setup for plugin-owned operational models."""
import django
import sys
from django.conf import settings

from tests.plugin.netbox_stubs import core

sys.modules.setdefault("core", core)


def pytest_configure():
    if settings.configured:
        return
    settings.configure(
        SECRET_KEY="test-only-not-a-credential",
        DEBUG=True,
        ALLOWED_HOSTS=["testserver"],
        INSTALLED_APPS=[
            "django.contrib.auth", "django.contrib.contenttypes",
            "django.contrib.sessions", "django.contrib.messages",
            "django.contrib.staticfiles", "tests.plugin.netbox_stubs.core.CoreConfig", "netbox_sync_plugin",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        ROOT_URLCONF="netbox_sync_plugin.urls",
        TEMPLATES=[{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "DIRS": [str(__import__("pathlib").Path(__file__).parent / "templates")], "APP_DIRS": True,
            "OPTIONS": {"context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]},
        }],
        STATIC_URL="/static/",
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        NETBOX_SYNC_TESTING=True,
        PLUGINS_CONFIG={"netbox_sync_plugin": {"worker_timeout": 43200}},
    )
    django.setup()
