from django.urls import path

from . import mail_views

urlpatterns = [
    path("management/email/", mail_views.mail_history, name="mail-history"),
    path("management/email/compose/", mail_views.mail_compose, name="mail-compose"),
    path("management/email/<uuid:request_id>/", mail_views.mail_detail, name="mail-detail"),
]
