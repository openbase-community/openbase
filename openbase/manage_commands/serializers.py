from __future__ import annotations

from openbase.config.serializers import (
    BaseDataclassSerializer,
)
from openbase.manage_commands.models import ManageCommand


class ManageCommandSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = ManageCommand
