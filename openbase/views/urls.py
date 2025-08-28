from rest_framework.routers import DefaultRouter

from .views import DjangoViewSetViewSet

router = DefaultRouter()
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/views",
    DjangoViewSetViewSet,
    basename="viewset",
)
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/views",
    DjangoViewSetViewSet,
    basename="viewset2",
)
router.register(
    r"projects/local/views",
    DjangoViewSetViewSet,
    basename="viewset3",
)


urlpatterns = router.urls
