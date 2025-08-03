from rest_framework.routers import DefaultRouter

from .views import ManageCommandViewSet

router = DefaultRouter()
router.register(
    r"projects/current/app-packages/(?P<app_package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/manage-commands",
    ManageCommandViewSet,
    basename="manage-command",
)

urlpatterns = router.urls
