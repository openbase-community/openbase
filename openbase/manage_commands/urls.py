from rest_framework.routers import DefaultRouter

from .views import ManageCommandViewSet

router = DefaultRouter()
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/commands",
    ManageCommandViewSet,
    basename="manage-command",
)
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/commands",
    ManageCommandViewSet,
    basename="manage-command",
)
router.register(
    r"projects/current/commands",
    ManageCommandViewSet,
    basename="manage-command",
)


urlpatterns = router.urls
