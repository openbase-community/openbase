from openbase.config.serializers import BasicSourceFileSerializer
from openbase.config.viewsets import BaseMemoryViewSet
from openbase.manage_commands.models import ManageCommand

from .serializers import ManageCommandSerializer


class ManageCommandViewSet(BaseMemoryViewSet):
    lookup_field = "name"
    lookup_url_kwarg = "name"

    def get_serializer_class(self):
        if self.action == "list":
            return BasicSourceFileSerializer
        return ManageCommandSerializer

    def get_queryset(self):
        return ManageCommand.objects.filter(**self.kwargs)
