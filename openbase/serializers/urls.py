from rest_framework.routers import DefaultRouter

from .views import DjangoSerializerViewSet

router = DefaultRouter()
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/serializers",
    DjangoSerializerViewSet,
    basename="serializer",
)
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/serializers",
    DjangoSerializerViewSet,
    basename="serializer",
)
router.register(
    r"projects/local/serializers",
    DjangoSerializerViewSet,
    basename="serializer",
)


urlpatterns = router.urls
