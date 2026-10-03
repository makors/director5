from django.urls import include, path

from . import views
from .docs_urls import urlpatterns as docs_patterns
from .governance_urls import urlpatterns as governance_patterns
from .images_urls import urlpatterns as image_patterns
from .mail_urls import urlpatterns as mail_patterns
from .management_views import urlpatterns as management_patterns
from .runtime_urls import urlpatterns as runtime_patterns
from .settings_urls import urlpatterns as settings_patterns

app_name = "sites"

urlpatterns = [
    path("<int:site_id>/files/", include("director.apps.sites.file_urls", namespace="files")),
    path("", views.index, name="index"),
    path("dashboard/<int:site_id>", views.site_dashboard, name="dashboard"),
    path("create/", views.create_site, name="create"),
    path("create/personal/", views.create_site, {"personal": True}, name="create-personal"),
    path("delete/<int:site_id>", views.delete_site, name="delete"),
    path("restart/<int:site_id>", views.restart_site, name="restart"),
    path("rebuild/<int:site_id>", views.rebuild_site, name="rebuild"),
    path("retry/<int:site_id>", views.retry_operation, name="retry"),
]
urlpatterns += (
    image_patterns
    + runtime_patterns
    + settings_patterns
    + governance_patterns
    + management_patterns
    + docs_patterns
    + mail_patterns
)
