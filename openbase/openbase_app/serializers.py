from rest_framework import serializers

from openbase.config.serializers import BaseDataclassSerializer
from openbase.openbase_app.models import AppPackage, DjangoApp, Project


class DjangoAppSerializer(BaseDataclassSerializer):
    class Meta:
        dataclass = DjangoApp
        fields = ["path", "package_name", "name"]


class AppPackageSerializer(BaseDataclassSerializer):
    django_apps = DjangoAppSerializer(many=True)

    class Meta:
        dataclass = AppPackage
        fields = ["path", "name", "django_apps"]


class ProjectSerializer(serializers.ModelSerializer):
    app_packages = AppPackageSerializer(many=True)

    class Meta:
        model = Project
        fields = ["id", "path_str", "name", "app_packages"]
