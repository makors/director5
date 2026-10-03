from django.urls import path

from . import docs_views

urlpatterns = [
    path("docs/", docs_views.docs_index, name="docs-index"),
    path("docs/search/", docs_views.docs_search, name="docs-search"),
    path("docs/<path:page>/", docs_views.docs_page, name="docs-page"),
]
