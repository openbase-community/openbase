from rest_framework.routers import DefaultRouter

from .views import TaskiqTaskViewSet

router = DefaultRouter()
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/tasks",
    TaskiqTaskViewSet,
    basename="task",
)
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/tasks",
    TaskiqTaskViewSet,
    basename="task",
)
router.register(
    r"projects/current/tasks",
    TaskiqTaskViewSet,
    basename="task",
)


urlpatterns = router.urls