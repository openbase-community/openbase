from rest_framework.routers import DefaultRouter

from .views import DjangoModelViewSet

router = DefaultRouter()
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/models",
    DjangoModelViewSet,
    basename="model",
)
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/models",
    DjangoModelViewSet,
    basename="model2",
)
router.register(
    r"projects/local/models",
    DjangoModelViewSet,
    basename="model3",
)


urlpatterns = router.urls
