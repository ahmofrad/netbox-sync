"""Read-only Operations pages and a separate confirmed POST execution boundary."""
from django.contrib import messages
from django.contrib.messages.api import MessageFailure
from django.http import HttpResponseRedirect
from django.urls import NoReverseMatch, reverse
from django.views import View
from django.views.generic import TemplateView
from django.utils import timezone

from .health import InventoryHealth
from .models import SyncRun, SyncState
from .services import SyncService

try:
    from utilities.views import ContentTypePermissionRequiredMixin
except ImportError:
    from django.contrib.auth.mixins import AccessMixin

    class ContentTypePermissionRequiredMixin(AccessMixin):
        """Isolated-test fallback for NetBox's model permission mixin."""
        def has_permission(self):
            return self.request.user.has_perm(self.get_required_permission())

        def dispatch(self, request, *args, **kwargs):
            if not request.user.is_authenticated or not self.has_permission():
                return self.handle_no_permission()
            return super().dispatch(request, *args, **kwargs)


COMPLETED_STATUSES = ("succeeded", "partial", "failed", "cancelled")


def operations_url():
    try:
        return reverse("plugins:netbox_sync_plugin:operations")
    except NoReverseMatch:
        return reverse("operations")


def current_health(request):
    """Health is restricted independently from permission to view private runs."""
    if not request.user.has_perm("dcim.view_device"):
        return None
    try:
        from dcim.models import Device
        return InventoryHealth.from_queryset(Device.objects.restrict(request.user, "view"))
    except (ImportError, AttributeError):
        return None


def _notify(request, level, text):
    try:
        messages.add_message(request, level, text)
    except MessageFailure:
        # Direct view unit tests do not install the session/message middleware.
        pass


class OperationsView(ContentTypePermissionRequiredMixin, TemplateView):
    template_name = "netbox_sync_plugin/operations.html"

    def get_required_permission(self):
        return "netbox_sync_plugin.view_syncrun"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        history_error = False
        try:
            runs = list(SyncRun.objects.filter(
                stage="pipeline", status__in=COMPLETED_STATUSES,
            ).order_by("-finished_at", "-pk")[:20])
        except Exception:
            runs = []
            history_error = True
        selected = None
        requested = self.request.GET.get("run", "")
        if requested.isdigit():
            selected = next((run for run in runs if run.pk == int(requested)), None)
        selected = selected or (runs[0] if runs else None)
        families = []
        issues = []
        if selected is not None:
            try:
                families = list(selected.families.all().order_by("family_key"))
                for family in families:
                    family.changes = family.created + family.updated + family.adopted
                issues = list(selected.issues.all().order_by("-pk")[:50])
            except Exception:
                history_error = True
        try:
            service = SyncService()
            state = SyncState.objects.first()
            active = service.has_active_run()
            next_scheduled = service.next_scheduled_run(state)
        except Exception:
            state = None
            active = None
            next_scheduled = None
            history_error = True
        heartbeat = getattr(state, "last_heartbeat", None)
        heartbeat_state = ("unavailable" if heartbeat is None else
                           "fresh" if timezone.now() - heartbeat <= timezone.timedelta(minutes=10)
                           else "stale")
        worker_state = getattr(active, "status", None) or (
            "active" if heartbeat_state == "fresh" else "idle")
        latest_success = next((run for run in runs if run.status == "succeeded"), None)
        context.update({
            "runs": runs,
            "selected_run": selected,
            "families": families,
            "issues": issues,
            "state": state,
            "active_run": active,
            "next_scheduled_run": next_scheduled,
            "worker_state": worker_state,
            "heartbeat_state": heartbeat_state,
            "latest_success": latest_success,
            "history_error": history_error,
            "health": current_health(self.request),
            "can_run": self.request.user.has_perm("netbox_sync_plugin.run_syncrun"),
            "changes": ((selected.created + selected.updated + selected.adopted)
                        if selected else 0),
            "completion_percent": self._completion_percent(selected),
            "run_action": operations_url().replace("operations/", "run/"),
        })
        return context

    @staticmethod
    def _completion_percent(run):
        if run is None or not run.discovered:
            return 0
        completed = run.created + run.updated + run.adopted + run.unchanged
        return min(100, round(completed * 100 / run.discovered))


class RunSyncView(ContentTypePermissionRequiredMixin, View):
    def get_required_permission(self):
        return "netbox_sync_plugin.run_syncrun"

    def post(self, request):
        if request.POST.get("confirm") != "yes":
            _notify(request, messages.WARNING,
                    "Synchronization was not started. Explicit confirmation is required.")
            return HttpResponseRedirect(operations_url())
        try:
            run = SyncService().enqueue(trigger="manual", requester=request.user)
        except Exception:
            _notify(request, messages.ERROR,
                    "Synchronization could not be queued. Check worker and queue status.")
        else:
            if run is None:
                _notify(request, messages.WARNING,
                        "Synchronization was not queued; no new job was created.")
                return HttpResponseRedirect(operations_url())
            if run.status == "failed":
                _notify(request, messages.ERROR,
                        "Synchronization could not be queued. Check worker and queue status.")
            elif not getattr(run, "newly_enqueued", False):
                _notify(request, messages.INFO,
                        f"Synchronization run {run.pk} is already {run.status}; no duplicate was created.")
            else:
                _notify(request, messages.SUCCESS,
                        f"Synchronization run {run.pk} was queued.")
        return HttpResponseRedirect(operations_url())
