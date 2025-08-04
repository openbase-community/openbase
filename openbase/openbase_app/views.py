from rest_framework import viewsets

from openbase.openbase_app.models import AppPackage, DjangoApp, Project
from openbase.openbase_app.serializers import (
    AppPackageSerializer,
    DjangoAppSerializer,
    ProjectSerializer,
)


class DjangoAppViewSet(viewsets.ModelViewSet):
    serializer_class = DjangoAppSerializer

    def get_queryset(self):
        return DjangoApp.objects.filter(
            app_package_name=self.kwargs["app_package_name"]
        )


class AppPackageViewSet(viewsets.ModelViewSet):
    serializer_class = AppPackageSerializer

    def get_queryset(self):
        return AppPackage.objects.all()


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer

    def get_queryset(self):
        return [Project.objects.get_or_create_current()]

    def get_object(self):
        return Project.objects.get_or_create_current()
