from rest_framework.routers import DefaultRouter

from .views import DjangoUrlsViewSet

router = DefaultRouter()
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/urls",
    DjangoUrlsViewSet,
    basename="urls",
)
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/urls",
    DjangoUrlsViewSet,
    basename="urls",
)
router.register(
    r"projects/current/urls",
    DjangoUrlsViewSet,
    basename="urls",
)


urlpatterns = router.urls