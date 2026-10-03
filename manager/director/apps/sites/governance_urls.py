from django.urls import path

from . import governance

urlpatterns = [
    path("accept-guidelines/", governance.accept_guidelines, name="guidelines"),
    path("guidelines/", governance.read_guidelines, name="guidelines-read"),
    path("request/", governance.create_request, name="request-create"),
    path("request/status/", governance.request_status, name="request-status"),
    path("request/approve/teacher/", governance.teacher_requests, name="request-teacher"),
    path("request/approve/admin/", governance.admin_requests, name="request-admin"),
]
