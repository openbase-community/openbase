from openbase.config.serializers import (
    BaseDataclassSerializer,
)
from openbase.tasks.models import TaskiqTask


class TaskiqTaskSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = TaskiqTask