from rest_framework.routers import DefaultRouter

from .views import TaskiqTaskViewSet

router = DefaultRouter()
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/apps/(?P<app_name>[^/.]+)/tasks",
    TaskiqTaskViewSet,
    basename="task",
)
router.register(
    r"projects/local/packages/(?P<package_name>[^/.]+)/tasks",
    TaskiqTaskViewSet,
    basename="task2",
)
router.register(
    r"projects/local/tasks",
    TaskiqTaskViewSet,
    basename="task3",
)


urlpatterns = router.urls
