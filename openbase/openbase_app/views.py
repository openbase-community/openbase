from rest_framework import status, viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase.config.viewsets import BaseMemoryViewSet
from openbase.openbase_app.models import AppPackage, DjangoApp, Project
from openbase.openbase_app.serializers import (
    AppPackageSerializer,
    DjangoAppSerializer,
    ProjectSerializer,
)


class DjangoAppViewSet(BaseMemoryViewSet):
    serializer_class = DjangoAppSerializer

    def get_queryset(self):
        return DjangoApp.objects.filter(**self.kwargs)


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


@api_view(['POST'])
def file_change_notification(request):
    """Handle file change notifications from the directory watcher."""
    change_type = request.data.get('change_type')
    file_path = request.data.get('file_path')
    
    # Print the change for now
    if change_type == 'created':
        print(f"  + {file_path}")
    elif change_type == 'modified':
        print(f"  ~ {file_path}")
    elif change_type == 'deleted':
        print(f"  - {file_path}")
    elif change_type == 'moved':
        dest_path = request.data.get('dest_path')
        print(f"  → {file_path} -> {dest_path}")
    
    return Response({"message": "File change notification received"}, status=status.HTTP_200_OK)
