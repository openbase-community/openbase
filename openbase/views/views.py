from openbase.config.viewsets import BaseMemoryViewSet
from openbase.views.models import DjangoViewSet

from .serializers import DjangoViewSetSerializer


class DjangoViewSetViewSet(BaseMemoryViewSet):
    serializer_class = DjangoViewSetSerializer

    def get_queryset(self):
        return DjangoViewSet.objects.filter(**self.kwargs)