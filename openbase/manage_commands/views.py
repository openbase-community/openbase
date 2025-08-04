from rest_framework import viewsets

from openbase.config.serializers import BasicSourceFileSerializer
from openbase.manage_commands.models import ManageCommand

from .serializers import ManageCommandSerializer


class ManageCommandViewSet(viewsets.ModelViewSet):
    lookup_field = "name"
    lookup_url_kwarg = "name"

    def get_serializer_class(self):
        if self.action == "list":
            return BasicSourceFileSerializer
        return ManageCommandSerializer

    def get_queryset(self):
        used_kwargs = {**self.kwargs}
        used_kwargs.pop(self.lookup_url_kwarg)
        return ManageCommand.objects.filter(**used_kwargs)

    def get_object(self):
        return self.get_queryset().get(
            self.lookup_url_kwarg, self.kwargs[self.lookup_url_kwarg]
        )
