from django.urls import path

from . import database_views

app_name = "databases"
urlpatterns = [
    path("", database_views.database_detail, name="detail"),
    path("create/", database_views.create_database, name="create"),
    path("delete/", database_views.delete_database, name="delete"),
    path("rotate-password/", database_views.rotate_database_password, name="rotate_password"),
    path("query/", database_views.query_database, name="query"),
]
