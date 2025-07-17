from django.conf import settings
from rest_framework import serializers


# Input Serializers for operations
class RunManagementCommandSerializer(serializers.Serializer):
    command = serializers.CharField(required=True)
    args = serializers.ListField(
        child=serializers.CharField(), required=False, default=list, allow_empty=True
    )

    def validate_command(self, value):
        if value not in settings.ALLOWED_DJANGO_COMMANDS:
            raise serializers.ValidationError(
                f"Command '{value}' not allowed. Allowed commands: {sorted(settings.ALLOWED_DJANGO_COMMANDS)}"
            )
        return value

    def validate_args(self, value):
        for arg in value:
            # Prevent command injection by checking for dangerous characters
            if any(char in arg for char in [";", "&&", "||", "|", "`", "$", ">", "<"]):
                raise serializers.ValidationError(
                    f"Argument '{arg}' contains invalid characters"
                )
        return value


class CreateSuperuserSerializer(serializers.Serializer):
    username = serializers.CharField(required=True)
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True)


class CreateAppSerializer(serializers.Serializer):
    app_name = serializers.CharField(required=True)
    app_type = serializers.CharField(required=False, default="basic")
    boilerplate_data = serializers.DictField(required=False, default=dict)

    def validate_app_name(self, value):
        # Basic validation for app name
        if not value.isidentifier():
            raise serializers.ValidationError(
                "App name must be a valid Python identifier"
            )
        return value


class ClaudeCodeMessageSerializer(serializers.Serializer):
    message = serializers.CharField(required=True)
    system_prompt = serializers.CharField(required=False, default="You are a helpful assistant.")
    max_turns = serializers.IntegerField(required=False, default=1)


class SourceCodeModificationSerializer(serializers.Serializer):
    """Serializer for source code modifications using AST positions"""
    start_line = serializers.IntegerField(min_value=1)
    start_col = serializers.IntegerField(min_value=0)
    end_line = serializers.IntegerField(min_value=1)
    end_col = serializers.IntegerField(min_value=0)
    replacement = serializers.CharField()

    def validate(self, data):
        if data['end_line'] < data['start_line']:
            raise serializers.ValidationError("End line must be >= start line")
        if data['end_line'] == data['start_line'] and data['end_col'] < data['start_col']:
            raise serializers.ValidationError("End column must be >= start column on same line")
        return data


# Output Serializers for API responses
class EnvInfoSerializer(serializers.Serializer):
    django_project_dir = serializers.CharField()
    django_project_apps_dirs = serializers.ListField(child=serializers.CharField())
    api_prefix = serializers.CharField()


class CommandResultSerializer(serializers.Serializer):
    command = serializers.CharField()
    returncode = serializers.IntegerField()
    stdout = serializers.CharField()
    stderr = serializers.CharField()
    success = serializers.BooleanField()


class AppInfoSerializer(serializers.Serializer):
    name = serializers.CharField()
    path = serializers.CharField()
    apps_dir = serializers.CharField()


class AppsListSerializer(serializers.Serializer):
    apps = AppInfoSerializer(many=True)


class FileListItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    file = serializers.CharField()


class FileListSerializer(serializers.Serializer):
    tasks = FileListItemSerializer(many=True, required=False)
    commands = FileListItemSerializer(many=True, required=False)


# AST-related serializers
class ASTNodeSerializer(serializers.Serializer):
    _nodetype = serializers.CharField()
    # Dynamic fields will be added based on node type
    
    def to_representation(self, instance):
        # Return the AST node data as-is since it's already in dict format
        return instance


class ModelFieldSerializer(serializers.Serializer):
    field_name = serializers.CharField()
    field_type = serializers.CharField()
    field_kwargs = serializers.DictField()
    help_text = serializers.CharField(allow_blank=True, required=False)
    verbose_name = serializers.CharField(allow_blank=True, required=False)
    choices = serializers.ListField(required=False)


class ModelMethodSerializer(serializers.Serializer):
    method_name = serializers.CharField()
    method_type = serializers.CharField()
    args = serializers.ListField()
    docstring = serializers.CharField(allow_null=True, required=False)
    body_source = serializers.CharField(required=False)
    decorators = serializers.ListField(required=False)


class ModelSerializer(serializers.Serializer):
    model_name = serializers.CharField()
    base_classes = serializers.ListField()
    docstring = serializers.CharField(allow_null=True, required=False)
    fields = ModelFieldSerializer(many=True)
    methods = ModelMethodSerializer(many=True)
    meta_class = serializers.DictField(required=False)


class ModelsResponseSerializer(serializers.Serializer):
    file_path = serializers.CharField()
    models = ModelSerializer(many=True)
    imports = serializers.ListField()


class ViewSerializer(serializers.Serializer):
    view_name = serializers.CharField()
    view_type = serializers.CharField()
    base_classes = serializers.ListField()
    docstring = serializers.CharField(allow_null=True, required=False)
    methods = serializers.ListField()
    decorators = serializers.ListField(required=False)


class ViewsResponseSerializer(serializers.Serializer):
    file_path = serializers.CharField()
    views = ViewSerializer(many=True)
    imports = serializers.ListField()


class SerializerFieldSerializer(serializers.Serializer):
    field_name = serializers.CharField()
    field_type = serializers.CharField()
    field_kwargs = serializers.DictField()


class DRFSerializerSerializer(serializers.Serializer):
    serializer_name = serializers.CharField()
    base_classes = serializers.ListField()
    docstring = serializers.CharField(allow_null=True, required=False)
    fields = SerializerFieldSerializer(many=True)
    methods = serializers.ListField()


class SerializersResponseSerializer(serializers.Serializer):
    file_path = serializers.CharField()
    serializers = DRFSerializerSerializer(many=True)
    imports = serializers.ListField()


class URLPatternSerializer(serializers.Serializer):
    pattern = serializers.CharField()
    view = serializers.CharField()
    name = serializers.CharField(allow_null=True, required=False)
    kwargs = serializers.DictField(required=False)


class URLsResponseSerializer(serializers.Serializer):
    file_path = serializers.CharField()
    urlpatterns = URLPatternSerializer(many=True)
    imports = serializers.ListField()


class TaskInfoSerializer(serializers.Serializer):
    task_name = serializers.CharField()
    task_type = serializers.CharField()
    docstring = serializers.CharField(allow_null=True, required=False)
    decorators = serializers.ListField()
    args = serializers.ListField()


class TasksResponseSerializer(serializers.Serializer):
    file_path = serializers.CharField()
    tasks = TaskInfoSerializer(many=True)
    imports = serializers.ListField()


class CommandInfoSerializer(serializers.Serializer):
    command_name = serializers.CharField()
    base_classes = serializers.ListField()
    docstring = serializers.CharField(allow_null=True, required=False)
    methods = serializers.ListField()


class CommandsResponseSerializer(serializers.Serializer):
    file_path = serializers.CharField()
    commands = CommandInfoSerializer(many=True)
    imports = serializers.ListField()


class ApiPrefixSerializer(serializers.Serializer):
    api_prefix = serializers.CharField()


class MessageSerializer(serializers.Serializer):
    message = serializers.CharField()


class AppCreatedSerializer(serializers.Serializer):
    message = serializers.CharField()
    app_dir = serializers.CharField()


class ClaudeCodeResponseSerializer(serializers.Serializer):
    response = serializers.CharField()
    conversation_id = serializers.CharField(required=False)
    turn_count = serializers.IntegerField(required=False)

