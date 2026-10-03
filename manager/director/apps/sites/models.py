from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator, RegexValidator
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Lower
from django.utils import timezone

if TYPE_CHECKING:
    from ..users.models import User


class OperationInProgressError(Exception):
    """A site already has an operation that has not finished."""


class SiteQuerySet(models.QuerySet):
    def filter_visible(self, user: User) -> Self:
        """Only show the sites the user can access.

        .. seealso::

            Use :meth:`filter_editable` for filtering for sites the user can edit.
        """
        if user.is_superuser:
            return self
        return self.filter(users=user)

    def filter_editable(self, user: User) -> Self:
        """Only show the sites the user can edit.

        .. seealso::

            Use :meth:`filter_visible` for filtering for sites the user can see.
        """
        query = self.filter_visible(user)
        if not user.is_superuser:
            query = query.filter(availability__in=["enabled", "not-served"])
        return query


class Site(models.Model):
    """A representation of the information for a specific website.

    Sites can either be static, which would be the case if they're serving
    html/css/js that doesn't change, or dynamic, which would be the case of a webserver
    of some kind (Django, NodeJS, Axum, etc).

    Depending on the purpose, the site will be deployed at different urls.

    At the application level, we validate that databases can *only* be added to dynamic sites.
    However, to prevent accidental loss of data, we don't delete the database after a change from
    dynamic -> static.
    """

    TYPES = (("static", "Static"), ("dynamic", "Dynamic"))

    PURPOSES = (
        ("legacy", "Legacy"),
        ("user", "User"),
        ("project", "Project"),
        ("activity", "Activity"),
        ("other", "Other"),
    )

    AVAILABILITIES = [
        ("enabled", "Enabled (fully functional)"),
        ("not-served", "Not served publicly"),
        ("disabled", "Disabled (not served, only viewable/editable by admins)"),
    ]

    name = models.CharField(
        max_length=100,
        unique=True,
        # Don't replace these classes with "\w". That allows Unicode characters. We just want ASCII.
        validators=[
            MinLengthValidator(2),
            RegexValidator(
                regex=r"^[a-z0-9]+(-[a-z0-9]+)*$",
                message=(
                    "Site names must consist of lowercase letters, numbers, and dashes. "
                    "Dashes must go between two non-dash characters."
                ),
            ),
        ],
    )

    description = models.TextField(blank=True)
    admin_comments = models.TextField(blank=True, default="")
    custom_nginx_config = models.TextField(blank=True, default="")
    docker_image = models.ForeignKey("DockerImage", null=True, blank=True, on_delete=models.PROTECT)
    image_packages = models.TextField(blank=True, default="")
    image_write_run_script = models.BooleanField(default=False)

    mode = models.CharField(max_length=10, choices=TYPES)

    purpose = models.CharField(
        max_length=10,
        choices=PURPOSES,
        help_text="What the site was created for.",
    )

    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="site_set",
    )

    database = models.OneToOneField(
        "Database",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site",
    )

    availability = models.CharField(
        max_length=20,
        choices=AVAILABILITIES,
        default="enabled",
        help_text="Controls who can access the site",
    )

    objects = SiteQuerySet.as_manager()

    id: int
    domain_set: models.QuerySet[Domain]
    operation: Operation

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.name in getattr(settings, "WHITELISTED_SITE_NAMES", []):
            return
        if self.name in getattr(settings, "BLACKLISTED_SITE_NAMES", []):
            raise ValidationError({"name": "This site name is not allowed."})
        for pattern in getattr(settings, "BLACKLISTED_SITE_REGEXES", []):
            try:
                if re.search(pattern, self.name):
                    raise ValidationError({"name": "This site name is not allowed."})
            except re.error as exc:
                raise ValidationError(
                    {"name": "Site name validation is misconfigured. Contact an administrator."}
                ) from exc

    @property
    def is_served(self) -> bool:
        return self.availability == "enabled"

    @property
    def status(self) -> str:
        try:
            return self.operation.status
        except Operation.DoesNotExist:
            return {"enabled": "ready", "not-served": "paused"}.get(self.availability, "disabled")

    @property
    def status_label(self) -> str:
        return {
            "queued": "Queued",
            "running": "In progress",
            "failed": "Needs attention",
            "ready": "Ready",
            "paused": "Not served",
            "disabled": "Disabled",
        }[self.status]

    @property
    def sites_url(self) -> str:
        """Return the default URL where the site is served."""
        default = settings.SITE_URL_FORMATS[None]
        url = settings.SITE_URL_FORMATS.get(self.purpose, default).format(self.name)
        if "://" not in url:
            hostname = urlsplit("//" + url).hostname or ""
            scheme = (
                "http" if hostname == "localhost" or hostname.endswith(".localhost") else "https"
            )
            url = f"{scheme}://{url}"
        return url

    def channels_group_name(self) -> str:
        """The name of the channel group for this site."""
        return f"site_{self.id}"

    def start_operation(
        self, ty: str, *, replace_failed: bool = False, expected_operation_id: int | None = None
    ) -> Operation:
        from . import operations

        try:
            with transaction.atomic():
                # Serialize operation creation on databases with row-level locking.
                Site.objects.select_for_update().get(pk=self.pk)
                current = Operation.objects.filter(site=self).first()
                if expected_operation_id is not None and (
                    current is None or current.pk != expected_operation_id
                ):
                    raise OperationInProgressError(
                        "This operation changed. Refresh the page before retrying."
                    )
                if current:
                    if not replace_failed or current.status != "failed":
                        raise OperationInProgressError(
                            "This site already has an operation in progress."
                        )
                    current.action_set.all().delete()
                    current.delete()
                op = Operation.objects.create(site=self, ty=ty)
        except IntegrityError as exc:
            raise OperationInProgressError(
                "This site already has an operation in progress."
            ) from exc
        operations.send_operation_updated_message(self)
        return op

    def list_domains(self) -> list[str]:
        """Returns all the domains for a site."""
        return [
            ("https://" + domain)
            for domain in self.domain_set.filter(status="active").values_list("domain", flat=True)
        ] + [self.sites_url]

    def serialize_resource_limits(self) -> dict[str, Any]:
        """Serialize the resource limits for the appservers."""
        limits = {
            "cpus": settings.DIRECTOR_RESOURCES_DEFAULT_CPUS,
            "memory": settings.DIRECTOR_RESOURCES_DEFAULT_MEMORY_LIMIT,
            "max_request_body_size": settings.DIRECTOR_RESOURCES_MAX_REQUEST_BODY,
        }
        try:
            custom = self.resource_limits
        except SiteResourceLimits.DoesNotExist:
            return limits
        for key in limits:
            value = getattr(custom, key)
            if value not in (None, "", 0):
                limits[key] = value
        return limits

    def serialize_for_appserver(self) -> dict[str, Any]:
        routes = []
        for url in self.list_domains():
            parsed = urlsplit(url)
            path_prefix = parsed.path.rstrip("/") + "/"
            routes.append({"host": parsed.hostname, "path_prefix": path_prefix})
        data = {
            "pk": self.id,
            "hosts": list(dict.fromkeys(route["host"] for route in routes)),
            "routes": routes,
            "is_served": self.is_served,
            "type_": self.mode,
            "resource_limits": self.serialize_resource_limits(),
        }
        if self.database is not None:
            data["db"] = self.database.serialize_for_appserver()
        data["custom_nginx_config"] = self.custom_nginx_config
        return data


class DockerImageSetupCommand(models.Model):
    name = models.CharField(max_length=100)
    command = models.TextField()
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class DockerImage(models.Model):
    name = models.CharField(max_length=255, unique=True)
    friendly_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_user_visible = models.BooleanField(default=False)
    setup_commands = models.ManyToManyField(DockerImageSetupCommand, blank=True)
    base_install_command = models.TextField(blank=True)
    install_command_prefix = models.CharField(max_length=255, blank=True)
    run_script_template = models.TextField(blank=True)

    def __str__(self):
        return self.friendly_name or self.name


class SiteResourceLimits(models.Model):
    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="resource_limits")
    cpus = models.FloatField(null=True, blank=True)
    memory = models.CharField(max_length=32, blank=True)
    max_request_body_size = models.BigIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"Resource limits for {self.site.name}"


class SitePendingUser(models.Model):
    username = models.CharField(max_length=32, unique=True)
    sites = models.ManyToManyField(Site, related_name="pending_users")

    def __str__(self):
        return self.username


class SiteRequest(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="requested_sites"
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="site_requests"
    )
    request_date = models.DateTimeField(auto_now_add=True)
    teacher_approval = models.BooleanField(null=True, default=None)
    admin_approval = models.BooleanField(null=True, default=None)
    admin_comments = models.TextField(blank=True)
    private_admin_comments = models.TextField(blank=True)
    activity = models.CharField(max_length=32)
    extra_information = models.TextField(blank=True)

    def __str__(self):
        return self.activity


class DatabaseHost(models.Model):
    """The different types of databases offered to users.

    Users should never be able to see or interact with this model directly,
    but rather through the :class:`Database` model.
    """

    # These should be capable of being put in a database URL
    # Hence "postgres", not "postgresql"
    DBMS_TYPES = [
        ("postgres", "PostgreSQL"),
        ("mysql", "MySQL"),
    ]

    # These parameters are passed to the containers to tell them how to connect to the database.
    hostname = models.CharField(max_length=255)
    port = models.PositiveIntegerField()

    dbms = models.CharField(max_length=16, choices=DBMS_TYPES)

    # These are used by the appservers to connect to and administer the database.
    # If either is unset, 'hostname' and 'port' are used, respectively. (Note that setting
    # admin_port=0 will also force a fallback on 'port'.)
    # If admin_hostname begins with a "/", it will be interpreted as a Unix socket path.
    admin_hostname = models.CharField(max_length=255, null=False, blank=True, default="")
    admin_port = models.PositiveIntegerField(null=True, blank=True, default=None)

    admin_username = models.CharField(max_length=255, null=False, blank=False)
    admin_password = models.CharField(max_length=255, null=False, blank=False)

    def __str__(self):
        return f"{self.dbms}://{self.hostname}:{self.port}"


class Database(models.Model):
    """A database for a specific site."""

    host = models.ForeignKey(DatabaseHost, on_delete=models.CASCADE)
    password = models.CharField(max_length=255, null=False, blank=False)
    provisioned = models.BooleanField(default=False)
    pending_password = models.CharField(max_length=255, blank=True, default="")

    site: Site

    def __str__(self) -> str:
        return self.redacted_db_url

    @property
    def username(self) -> str:
        return f"site_{self.site.id}"

    @property
    def redacted_db_url(self) -> str:
        return f"{self.host.dbms}://{self.username}:***@{self.host.hostname}:{self.host.port}/{self.username}"

    def serialize_for_appserver(self) -> dict[str, str]:
        return {
            "url": self.redacted_db_url,
            "name": self.username,
            "username": self.username,
            "password": self.password,
        }


class Domain(models.Model):
    """Represents a custom (non-`sites.tjhsst.edu`) domain.

    `sites.tjhsst.edu` domains MUST be set up by creating a site with that name.

    Note: It must be ensured that *.tjhsst.edu domains can only be set up by Director admins.

    """

    STATUSES = [
        # Enabled (most domains)
        ("active", "Active"),
        # Disabled (respected in generation of configuration, but currently no provisions for
        # setting domains to inactive)
        ("inactive", "Inactive"),
        ("removing", "Removal pending"),
        # This domain was removed from the Site it was added to. All records of it should be
        # removed.
        ("deleted", "Deleted"),
        # Reserved domains we don't want people to use for legal/policy reasons (these should always
        # have site=None)
        ("blocked", "Blocked"),
    ]

    # Should ONLY be None for deleted or blocked domains
    site = models.ForeignKey(Site, null=True, on_delete=models.PROTECT)

    domain = models.CharField(
        max_length=255,
        validators=[
            RegexValidator(
                regex=r"^(?!(.*\.)?sites\.tjhsst\.edu$)"
                r"[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)+$$",
                message="Invalid domain. (Note: You can only have one sites.tjhsst.edu domain, and "
                "it must match the name of your site.)",
            )
        ],
    )

    created_time = models.DateTimeField(auto_now_add=True, null=False)
    creating_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    status = models.CharField(max_length=8, choices=STATUSES, default="active")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("domain"),
                condition=models.Q(status__in=["active", "inactive", "blocked", "removing"]),
                name="unique_reserved_domain",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.domain} ({self.site})"


class Operation(models.Model):
    """A series of actions being performed on a site.

    Examples:
        * The initial site creation process
        * Renaming a site
        * Changing a site type

    .. note::

        All state for an :class:`Operation` is in the Celery task. The database entries are only for easier inspection
        of running/failed tasks, so restarting/continuing a failed Operation is not possible. Instead, one
        should try running the "Fix site" task, which does its best to ensure that the site is set up properly.
    """

    OPERATION_TYPES = [
        # Create a site (no database)
        ("create_site", "Creating site"),
        # Rename the site. Changes the site name and the default domain name.
        ("rename_site", "Renaming site"),
        # Change the site name and domain names
        ("edit_site_names", "Changing site name/domains"),
        # Change the site type (example: static -> dynamic)
        ("change_site_type", "Changing site type"),
        # Create a database for the site
        ("create_site_database", "Creating site database"),
        # Delete a database for the site
        ("delete_site_database", "Deleting site database"),
        # Regenerate the database password.
        ("regen_site_secrets", "Regenerating site secrets"),
        # Updating the site's resource limits
        ("update_resource_limits", "Updating site resource limits"),
        # Updating something about the site's Docker image
        ("update_docker_image", "Updating site Docker image"),
        # Delete a site, its files, its database, its Docker image, etc.
        ("delete_site", "Deleting site"),
        # Restart a site's swarm service
        ("restart_site", "Restarting site"),
        # Tries to ensure everything is correct. Builds the Docker image, and updates the Docker service.
        ("fix_site", "Attempting to fix site"),
        ("regen_nginx_config", "Updating routing configuration"),
        ("update_availability", "Updating public access"),
    ]

    site = models.OneToOneField(Site, null=False, on_delete=models.PROTECT)
    ty = models.CharField(max_length=24, choices=OPERATION_TYPES, verbose_name="type")
    created_time = models.DateTimeField(auto_now_add=True, null=False)
    started_time = models.DateTimeField(null=True)

    def __str__(self) -> str:
        return f"{type(self).__name__}: {self.ty}"

    @property
    def has_started(self) -> bool:
        return self.started_time is not None

    @property
    def kind_label(self) -> str:
        return "Rebuild and deploy" if self.ty == "fix_site" else self.get_ty_display()

    @property
    def status(self) -> str:
        actions = list(self.action_set.all())
        if any(action.result is False for action in actions):
            return "failed"
        return "running" if self.has_started else "queued"

    @property
    def status_label(self) -> str:
        return {"queued": "Queued", "running": "In progress", "failed": "Failed"}[self.status]

    @property
    def progress(self) -> int:
        actions = list(self.action_set.all())
        if not actions:
            return 0
        return round(100 * sum(action.result is True for action in actions) / len(actions))

    def list_actions_in_order(self) -> models.QuerySet[Action]:
        return self.action_set.order_by("id")


class Action(models.Model):
    """An individual task in an operation.

    A representation of the state of a single action in an operation. For
    example, if the operation is fixing docker, the actions might be

    * "rebuild_docker_image"
    * "restart_docker_service"
    """

    operation = models.ForeignKey(Operation, null=False, on_delete=models.PROTECT)

    # Example: "update_nginx_config"
    slug = models.CharField(
        max_length=40,
        null=False,
        blank=False,
        validators=[MinLengthValidator(4), RegexValidator(regex=r"^[a-z]+(_[a-z]+)+$")],
    )
    # May be displayed to the user for progress updates. Example: "Updating Nginx config"
    name = models.CharField(max_length=80, null=False, blank=False)
    # Time this action was started. Only None if it hasn't been started yet.
    started_time = models.DateTimeField(null=True)

    # None=Not finished, True=Successful, False=Failed
    result = models.BooleanField(null=True, default=None)

    message = models.TextField(
        null=False,
        blank=True,
        help_text=(
            "Message describing the actions taken (and/or what failed). "
            "Should always be set to allow for easier debugging.\n"
            "Only visible to superusers."
        ),
    )

    user_message = models.TextField(
        null=False,
        blank=True,
        help_text="User-facing message describing the actions taken (and/or what failed).",
    )

    # Operations that fail because of failures in Actions with this field set to True will not
    # be exported in the Prometheus metrics, and they will have a special note on the operations
    # page.
    # The main practical use of this is for building the Docker image.
    user_recoverable = models.BooleanField(
        null=False,
        default=False,
        help_text="Can the user recover from this failure?",
    )

    def __str__(self) -> str:
        return f"{type(self).__name__}: {self.name}"

    def start_action(self) -> None:
        self.started_time = timezone.localtime()
        self.save(update_fields=["started_time"])
