from rest_framework.routers import DefaultRouter

from .views import DjangoUrlsViewSet

router = DefaultRouter()
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/urls",
    DjangoUrlsViewSet,
    basename="urls",
)
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/urls",
    DjangoUrlsViewSet,
    basename="urls2",
)
router.register(
    r"projects/local/urls",
    DjangoUrlsViewSet,
    basename="urls3",
)


urlpatterns = router.urls
