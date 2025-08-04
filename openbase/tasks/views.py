from openbase.config.viewsets import BaseMemoryViewSet
from openbase.tasks.models import TaskiqTask

from .serializers import TaskiqTaskSerializer


class TaskiqTaskViewSet(BaseMemoryViewSet):
    serializer_class = TaskiqTaskSerializer

    def get_queryset(self):
        return TaskiqTask.objects.filter(**self.kwargs)