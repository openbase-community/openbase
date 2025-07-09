from django.conf import settings
from rest_framework import serializers


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

