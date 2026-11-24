"""Private synchronization history; reconciliation still uses pynetbox only."""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from netbox_sync.report import sanitize_text
from .constants import COUNTER_FIELDS, STAGE_CHOICES, STATUS_CHOICES, TRIGGER_CHOICES


try:
    from netbox.models.features import JobsMixin
except ImportError:
    class JobsMixin(models.Model):
        """Test fallback matching NetBox's job feature relation."""
        jobs = GenericRelation("core.Job", content_type_field="object_type",
                               object_id_field="object_id", for_concrete_model=False)

        class Meta:
            abstract = True


class SyncState(JobsMixin, models.Model):
    key = models.CharField(max_length=32, unique=True, default="default")
    schedule_enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=1440, validators=[MinValueValidator(1)])
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    last_enqueue_error = models.CharField(max_length=500, blank=True)
    retention_runs = models.PositiveIntegerField(default=90, validators=[MinValueValidator(1)])
    max_issues_per_run = models.PositiveIntegerField(default=500, validators=[MinValueValidator(1)])
    issue_message_max_length = models.PositiveIntegerField(default=500, validators=[MinValueValidator(1)])

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(key="default"), name="sync_state_singleton")]


class SyncRun(models.Model):
    job = models.ForeignKey("core.Job", null=True, blank=True,
                            on_delete=models.SET_NULL, related_name="+")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="stages")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default="pipeline")
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    plugin_version = models.CharField(max_length=40, blank=True)
    source_revision = models.CharField(max_length=80, blank=True)
    assetexplorer_state = models.CharField(max_length=20, default="skipped")
    summary = models.CharField(max_length=500, blank=True)
    discovered = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    adopted = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    offline = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-queued_at", "-pk")
        indexes = [models.Index(fields=("status", "queued_at"))]
        permissions = [("run_syncrun", "Can enqueue synchronization")]
        default_permissions = ("add", "change", "delete", "view")

    def apply_snapshot(self, snapshot):
        for key in COUNTER_FIELDS:
            setattr(self, key, int(snapshot.get("counters", {}).get(key, 0)))
        self.summary = sanitize_text(snapshot.get("summary", self.summary))

    def finish(self, status, summary=""):
        self.status = status
        self.finished_at = timezone.now()
        if self.started_at:
            self.duration_seconds = max(0, int((self.finished_at - self.started_at).total_seconds()))
        self.summary = sanitize_text(summary)
        self.save()

    @classmethod
    def enforce_retention(cls, limit):
        completed = cls.objects.filter(status__in=("succeeded", "partial", "failed", "cancelled"))
        stale = completed.order_by("-finished_at", "-pk").values_list("pk", flat=True)[limit:]
        if stale:
            cls.objects.filter(pk__in=list(stale)).delete()


class SyncFamilyResult(models.Model):
    run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="families")
    family_key = models.CharField(max_length=64)
    display_label = models.CharField(max_length=100)
    status = models.CharField(max_length=20, default="succeeded")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    discovered = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    adopted = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    offline = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = (("run", "family_key"),)


class SyncIssue(models.Model):
    run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="issues")
    family = models.ForeignKey(SyncFamilyResult, null=True, blank=True,
                               on_delete=models.CASCADE, related_name="issues")
    device = models.PositiveBigIntegerField(null=True, blank=True)
    category = models.CharField(max_length=100)
    severity = models.CharField(max_length=20, default="error")
    source = models.CharField(max_length=200, blank=True)
    message = models.CharField(max_length=500)
    occurrences = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = (("run", "family", "category", "severity", "source", "message"),)

    @classmethod
    def record(cls, run, *, family=None, category, source="", message,
               severity="error", device=None, occurrences=1):
        state = SyncState.objects.first()
        limit = state.issue_message_max_length if state else 500
        clean = sanitize_text(message, limit)[:500]
        source = sanitize_text(source, 200)[:200]
        if run.issues.count() >= (state.max_issues_per_run if state else 500):
            existing = run.issues.filter(family=family, category=category, severity=severity,
                                         source=source, message=clean).first()
            if existing is None:
                return None
        issue, created = cls.objects.get_or_create(
            run=run, family=family, category=category, severity=severity,
            source=source, message=clean,
            defaults={"device": device, "occurrences": max(1, occurrences)})
        if not created:
            issue.occurrences += max(1, occurrences)
            issue.save(update_fields=("occurrences",))
        return issue
