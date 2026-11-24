"""NetBox 4.5.1 JobRunner integration on the plugin's dedicated queue."""
import logging
from datetime import timedelta

from django.utils import timezone

from .constants import QUEUE_NAME
from .models import SyncRun
from .services import SyncService

try:
    from core.choices import JobStatusChoices
    from core.exceptions import JobFailed
    from netbox.jobs import JobRunner
    from rq.timeouts import JobTimeoutException
except ImportError:
    class JobFailed(Exception):
        pass
    class JobStatusChoices:
        STATUS_FAILED = "failed"
        STATUS_ERRORED = "errored"
    class JobRunner:
        name = "NetBox synchronization"
        def __init__(self, job):
            self.job = job
            self.logger = None
    class JobTimeoutException(Exception):
        pass


class SyncPipelineJob(JobRunner):
    class Meta:
        name = "NetBox synchronization"

    queue_name = QUEUE_NAME
    job_timeout = 86400

    def __init__(self, job):
        super().__init__(job)
        self.service = SyncService()

    @classmethod
    def handle(cls, job, *args, **kwargs):
        """Mirror NetBox lifecycle handling without losing queue configuration.

        NetBox 4.5.1's base recurrence omits queue_name and derives timeout from
        an attribute SyncState does not have. Both are explicit here on every
        recurrence, along with the original pipeline arguments.
        """
        try:
            job.start()
            cls(job).run(*args, **kwargs)
            job.terminate()
        except JobFailed:
            logging.getLogger("netbox.jobs").warning("Synchronization job failed")
            job.terminate(status=JobStatusChoices.STATUS_FAILED)
        except Exception as exc:
            from netbox_sync.report import sanitize_text
            error = sanitize_text(f"{type(exc).__name__}: {exc}")
            job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=error)
            if isinstance(exc, JobTimeoutException):
                logging.getLogger("netbox.jobs").error(exc)
        finally:
            state = job.object
            enabled = state is None or not hasattr(state, "schedule_enabled") or state.schedule_enabled
            if job.interval and enabled:
                scheduled = max(
                    (job.scheduled or job.started) + timedelta(minutes=job.interval),
                    timezone.now() + timedelta(minutes=1),
                )
                timeout = SyncService().worker_timeout
                cls.enqueue(instance=state, name=job.name, user=job.user,
                            schedule_at=scheduled, interval=job.interval,
                            queue_name=QUEUE_NAME, job_timeout=timeout, **kwargs)

    def execute_run(self, include_assetexplorer=True):
        return SyncRun.objects.filter(job=self.job).first() or SyncRun.objects.create(
            trigger="scheduled" if self.job.interval else "manual", job=self.job,
            assetexplorer_state="pending" if include_assetexplorer else "skipped")

    def run(self, *args, include_assetexplorer=True, **kwargs):
        run = self.execute_run(include_assetexplorer=include_assetexplorer)
        try:
            result = self.service.execute(run)
        except KeyboardInterrupt:
            run.finish("cancelled", "Synchronization cancelled")
            raise JobFailed()
        except Exception as exc:
            from netbox_sync.report import sanitize_text
            run.finish("failed", f"Synchronization execution error: {type(exc).__name__}")
            raise JobFailed() from exc
        if not result.succeeded:
            run.finish(result.status, result.summary)
            raise JobFailed()
