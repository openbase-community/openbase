from openbase.config.serializers import (
    BaseDataclassSerializer,
)
from openbase.models.models import DjangoModel


class DjangoModelSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = DjangoModel
