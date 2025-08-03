from rest_framework import viewsets

from openbase.manage_commands.models import ManageCommand
from openbase.openbase_app.models import DjangoApp

from .serializers import ManageCommandSerializer


class ManageCommandViewSet(viewsets.ModelViewSet):
    serializer_class = ManageCommandSerializer

    def get_queryset(self):
        django_app = DjangoApp.objects.get(
            app_package_name=self.kwargs["app_package_name"],
            app_name=self.kwargs["app_name"],
        )
        return ManageCommand.objects.filter(django_app=django_app)

    def get_object(self):
        django_app = DjangoApp.objects.get(
            app_package_name=self.kwargs["app_package_name"],
            app_name=self.kwargs["app_name"],
        )
        return ManageCommand.objects.get(
            django_app=django_app,
            name=self.kwargs["pk"],
        )
