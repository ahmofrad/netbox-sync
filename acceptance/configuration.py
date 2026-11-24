"""Disposable-only NetBox configuration. No production endpoints or secrets."""
from os import environ

ALLOWED_HOSTS = ["*"]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": environ["DB_NAME"], "USER": environ["DB_USER"],
    "PASSWORD": environ["DB_PASSWORD"], "HOST": "postgres", "PORT": 5432,
    "CONN_MAX_AGE": 0,
}}
REDIS = {
    "tasks": {"HOST": "redis", "PORT": 6379, "DATABASE": 0, "SSL": False},
    "caching": {"HOST": "redis", "PORT": 6379, "DATABASE": 1, "SSL": False},
}
SECRET_KEY = environ["SECRET_KEY"]
API_TOKEN_PEPPERS = {1: environ["API_TOKEN_PEPPER_1"]}
PLUGINS = ["netbox_sync_plugin"]
PLUGINS_CONFIG = {"netbox_sync_plugin": {
    "schedule_enabled": False, "interval_minutes": 1, "retention_runs": 90,
    "worker_timeout": 60,
}}
CENSUS_REPORTING_ENABLED = False
RELEASE_CHECK_URL = None
ISOLATED_DEPLOYMENT = True
LOGIN_REQUIRED = True
TIME_ZONE = "UTC"
