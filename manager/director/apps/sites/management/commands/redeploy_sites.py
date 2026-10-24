"""Queue "Rebuild and deploy" for sites, e.g. to move them to a new site runtime."""

from django.core.management.base import BaseCommand

from ... import tasks
from ...models import OperationInProgressError, Site
from ...views import _queue_operation


class Command(BaseCommand):
    help = (
        "Rebuild and redeploy sites through the normal operation queue. After the "
        "Orchestrator switches to the Kubernetes runtime, this moves each site from its "
        "Swarm service to a Kubernetes pod."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "site_ids", nargs="*", type=int, help="Sites to redeploy (default: all)"
        )

    def handle(self, *args, site_ids, **options):
        sites = Site.objects.order_by("id")
        if site_ids:
            sites = sites.filter(id__in=site_ids)
        queued = skipped = 0
        for site in sites:
            try:
                operation = site.start_operation("fix_site")
            except OperationInProgressError:
                self.stdout.write(f"Skipped {site.name}: an operation is already in progress.")
                skipped += 1
                continue
            if _queue_operation(site, operation, tasks.rebuild_docker_image):
                queued += 1
            else:
                self.stderr.write(f"Could not queue {site.name}; retry it from its dashboard.")
                skipped += 1
        self.stdout.write(f"Queued {queued} site(s); skipped {skipped}.")
