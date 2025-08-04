from openbase.config.viewsets import BaseMemoryViewSet
from openbase.models.models import DjangoModel

from .serializers import DjangoModelSerializer


class DjangoModelViewSet(BaseMemoryViewSet):
    serializer_class = DjangoModelSerializer

    def get_queryset(self):
        return DjangoModel.objects.filter(**self.kwargs)
