from openbase.config.viewsets import BaseMemoryViewSet
from openbase.serializers.models import DjangoSerializer

from .serializers import DjangoSerializerSerializer


class DjangoSerializerViewSet(BaseMemoryViewSet):
    serializer_class = DjangoSerializerSerializer

    def get_queryset(self):
        return DjangoSerializer.objects.filter(**self.kwargs)