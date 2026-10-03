from django.contrib import admin

from .models import (
    Action,
    Database,
    DatabaseHost,
    DockerImage,
    DockerImageSetupCommand,
    Domain,
    Operation,
    Site,
    SitePendingUser,
    SiteRequest,
    SiteResourceLimits,
)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "mode", "purpose", "availability")
    list_filter = ("mode", "availability")
    search_fields = ("name",)


@admin.register(DatabaseHost)
class DatabaseHostAdmin(admin.ModelAdmin):
    list_display = ("hostname", "port", "dbms")
    list_filter = ("dbms",)
    search_fields = ("hostname",)


@admin.register(Database)
class DatabaseAdmin(admin.ModelAdmin):
    list_display = ("redacted_db_url", "site", "host__dbms")
    search_fields = ("host__hostname", "site__name")


admin.site.register(Operation)
admin.site.register(Action)
admin.site.register(Domain)
admin.site.register(DockerImage)
admin.site.register(DockerImageSetupCommand)
admin.site.register(SiteRequest)
admin.site.register(SitePendingUser)
admin.site.register(SiteResourceLimits)
