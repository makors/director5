from django.urls import path

from . import settings_views

urlpatterns = [
    path("settings/<int:site_id>/", settings_views.settings_view, name="settings"),
    path("settings/<int:site_id>/meta/", settings_views.edit_meta, name="settings-meta"),
    path("settings/<int:site_id>/name/", settings_views.edit_name, name="settings-name"),
    path("settings/<int:site_id>/type/", settings_views.edit_type, name="settings-type"),
    path("settings/<int:site_id>/admin/", settings_views.edit_admin, name="settings-admin"),
    path(
        "settings/<int:site_id>/members/add/", settings_views.add_member, name="settings-member-add"
    ),
    path(
        "settings/<int:site_id>/members/<int:user_id>/remove/",
        settings_views.remove_member,
        name="settings-member-remove",
    ),
    path(
        "settings/<int:site_id>/domains/add/", settings_views.add_domain, name="settings-domain-add"
    ),
    path(
        "settings/<int:site_id>/domains/<int:domain_id>/remove/",
        settings_views.remove_domain,
        name="settings-domain-remove",
    ),
]
