from openbase.config.serializers import (
    BaseDataclassSerializer,
)
from openbase.urls.models import DjangoUrls


class DjangoUrlsSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = DjangoUrls