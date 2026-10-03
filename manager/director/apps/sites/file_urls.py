from django.urls import path

from . import file_views

app_name = "files"

urlpatterns = [
    path("", file_views.browser, name="browser"),
    path("list/", file_views.list_files, name="list"),
    path("read/", file_views.read_file, name="read"),
    path("write/", file_views.write_file, name="write"),
    path("upload/", file_views.upload_files, name="upload"),
    path("mkdir/", file_views.make_directory, name="mkdir"),
    path("delete/", file_views.delete_file, name="delete"),
    path("move/", file_views.move_file, name="move"),
    path("chmod/", file_views.change_permissions, name="chmod"),
    path("download/", file_views.download_file, name="download"),
    path("archive/", file_views.download_archive, name="archive"),
]
