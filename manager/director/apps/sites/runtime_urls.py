from django.urls import path

from . import runtime_views

urlpatterns = [
    path("runtime/<int:site_id>/logs/", runtime_views.logs, name="logs"),
    path("runtime/<int:site_id>/logs/output/", runtime_views.logs_output, name="logs_output"),
    path("runtime/<int:site_id>/status/", runtime_views.runtime_status, name="runtime_status"),
    path("runtime/<int:site_id>/terminal/", runtime_views.terminal, name="terminal"),
    path(
        "runtime/<int:site_id>/terminal/session/",
        runtime_views.terminal_session,
        name="terminal_session",
    ),
    path("runtime/<int:site_id>/ssh/", runtime_views.ssh_access, name="ssh"),
]
