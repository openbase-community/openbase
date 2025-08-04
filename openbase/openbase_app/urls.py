from rest_framework.routers import DefaultRouter

from .views import AppPackageViewSet, DjangoAppViewSet, ProjectViewSet

router = DefaultRouter()
router.register(
    r"projects/current/apps",
    DjangoAppViewSet,
    basename="django-app",
)
router.register(
    r"projects/current/packages/(?P<package_name>[^/.]+)/apps",
    DjangoAppViewSet,
    basename="django-app",
)
router.register(r"projects/current/packages", AppPackageViewSet, basename="app-package")
router.register(r"projects", ProjectViewSet, basename="project")

urlpatterns = router.urls
