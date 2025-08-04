from rest_framework.routers import DefaultRouter

from .views import DjangoModelViewSet

router = DefaultRouter()
router.register(
    r"projects/current/app-packages/(?P<app_package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/models",
    DjangoModelViewSet,
    basename="model",
)
router.register(
    r"projects/current/app-packages/(?P<app_package_name>[^/.]+)/models",
    DjangoModelViewSet,
    basename="model",
)
router.register(
    r"projects/current/models",
    DjangoModelViewSet,
    basename="model",
)


urlpatterns = router.urls
