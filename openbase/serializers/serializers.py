from openbase.config.serializers import (
    BaseDataclassSerializer,
)
from openbase.serializers.models import DjangoSerializer


class DjangoSerializerSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = DjangoSerializer