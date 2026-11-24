"""Minimal core app used only for isolated plugin foreign-key tests."""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    label = "core"


default_app_config = "tests.plugin.netbox_stubs.core.CoreConfig"
