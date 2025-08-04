from openbase.config.viewsets import BaseMemoryViewSet
from openbase.urls.models import DjangoUrls

from .serializers import DjangoUrlsSerializer


class DjangoUrlsViewSet(BaseMemoryViewSet):
    serializer_class = DjangoUrlsSerializer

    def get_queryset(self):
        return DjangoUrls.objects.filter(**self.kwargs)