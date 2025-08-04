from openbase.config.serializers import (
    BaseDataclassSerializer,
)
from openbase.views.models import DjangoViewSet


class DjangoViewSetSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = DjangoViewSet