"""Plugin URLs; execution is deliberately POST-only."""
from django.urls import path

from . import views

app_name = "netbox_sync_plugin"

urlpatterns = [
    path("operations/", views.OperationsView.as_view(), name="operations"),
    path("run/", views.RunSyncView.as_view(), name="run"),
]
