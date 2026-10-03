from django.urls import path

from . import images_views as views

urlpatterns = [
    path("images/", views.image_catalog, name="image-catalog"),
    path("images/new/", views.image_edit, name="image-create"),
    path("images/<int:image_id>/edit/", views.image_edit, name="image-edit"),
    path("images/<int:image_id>/delete/", views.image_delete, name="image-delete"),
    path("images/setup/new/", views.setup_edit, name="setup-create"),
    path("images/setup/<int:command_id>/edit/", views.setup_edit, name="setup-edit"),
    path("images/setup/<int:command_id>/delete/", views.setup_delete, name="setup-delete"),
    path("image/<int:site_id>/", views.image_select, name="image-select"),
    path("resources/", views.resource_list, name="resource-list"),
    path("resources/<int:site_id>/", views.resource_edit, name="resource-edit"),
]
