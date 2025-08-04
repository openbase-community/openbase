from rest_framework.routers import DefaultRouter

from .views import DjangoViewSetViewSet

router = DefaultRouter()
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/viewsets",
    DjangoViewSetViewSet,
    basename="viewset",
)
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/viewsets",
    DjangoViewSetViewSet,
    basename="viewset",
)
router.register(
    r"projects/current/viewsets",
    DjangoViewSetViewSet,
    basename="viewset",
)


urlpatterns = router.urls