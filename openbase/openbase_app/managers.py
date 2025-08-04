from pathlib import Path

from openbase.config.managers import ListQuerySet, MemoryManager
from openbase.openbase_app.models import DjangoApp


class AppSpecificManager(MemoryManager):
    def list_for_app_path(self, app_path: Path, **kwargs):
        raise NotImplementedError("Subclasses must implement this method")

    def filter(self, **kwargs):
        app_name = kwargs.pop("app_name", None)
        if app_name is not None:
            django_apps = [DjangoApp.objects.get(name=app_name, **kwargs)]
        elif "package_name" in kwargs:
            django_apps = [
                django_app for django_app in DjangoApp.objects.filter(**kwargs)
            ]
        else:
            django_apps = DjangoApp.objects.all()

        results = []
        for django_app in django_apps:
            results.extend(
                self.list_for_app_path(
                    django_app.path,
                    app_name=django_app.name,
                    package_name=django_app.package_name,
                )
            )

        return ListQuerySet(results)
