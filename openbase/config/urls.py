from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from openbase.config import views

urlpatterns = [
    path("api/openbase/", include("openbase.openbase_app.urls")),
    path("api/openbase/", include("openbase.manage_commands.urls")),
    path("api/openbase/", include("openbase.models.urls")),
    path("api/openbase/", include("openbase.serializers.urls")),
    path("api/openbase/", include("openbase.tasks.urls")),
    path("api/openbase/", include("openbase.urls.urls")),
    path("api/openbase/", include("openbase.views.urls")),
]

if settings.DEBUG or True:
    urlpatterns.append(path("admin/", admin.site.urls))

urlpatterns.append(path("", views.proxy_or_fallback, name="proxy_or_fallback_root"))
urlpatterns.append(path("<path:path>", views.proxy_or_fallback, name="proxy_or_fallback"))
