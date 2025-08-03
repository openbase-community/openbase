from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import models


class DjangoAppManager:
    def filter(self, *, app_package_name: str) -> "DjangoApp":
        app_package = AppPackage.objects.get(name=app_package_name)
        return app_package.django_apps

    def get(self, *, app_package_name: str, app_name: str) -> "DjangoApp":
        app_package = AppPackage.objects.get(name=app_package_name)
        return next(
            django_app
            for django_app in app_package.django_apps
            if django_app.name == app_name
        )


@dataclass
class DjangoApp:
    path: Path
    package_name: str

    objects: DjangoAppManager = DjangoAppManager()

    @property
    def name(self) -> str:
        return self.path.name


class AppPackageManager:
    def all(self) -> list["AppPackage"]:
        return Project.objects.get_or_create_current().app_packages

    def get(self, *, name: str) -> "AppPackage":
        app_packages = Project.objects.get_or_create_current().app_packages
        return next(
            app_package for app_package in app_packages if app_package.name == name
        )


@dataclass
class AppPackage:
    path: Path

    objects: AppPackageManager = AppPackageManager()

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def python_source_path(self) -> Path:
        """
        This is the path, but slash the name with dashes replaced with underscores.  So if path is /path/to/my-app-package, this path will be /path/to/my-app-package/my_app_package.
        """
        return self.path / self.name.replace("-", "_")

    @property
    def django_apps(self) -> list[DjangoApp]:
        """
        This will be gotten from creating a list of the subdirectories in the python_source_path that contain an apps.py file.
        """
        django_apps = []
        for subdir in self.python_source_path.iterdir():
            if subdir.is_dir() and (subdir / "apps.py").exists():
                app = DjangoApp(path=subdir, package_name=self.name)
                django_apps.append(app)
        return django_apps


class ProjectManager(models.Manager):
    def get_or_create_current(self) -> "Project":
        return self.get_or_create(path_str=settings.OPENBASE_PROJECT_PATH)[0]


class Project(models.Model):
    path_str = models.CharField(max_length=512)

    objects: ProjectManager = ProjectManager()

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def path(self) -> Path:
        return Path(self.path_str)

    @property
    def app_packages(self) -> list[AppPackage]:
        """
        Open the path/web/workspace_requirements.txt.  It will have contents like:
        -e ../haven-api
        -e ../sim-api
        -e ../haven-sim

        For each line, cut out the -e ../ and create an AppPackage object.
        Return a list of AppPackage objects.
        """
        workspace_requirements_path = self.path / "web" / "workspace_requirements.txt"
        app_packages = []

        with open(workspace_requirements_path) as f:
            for line in f:
                line = line.strip()
                common_prefix = "-e ../"
                if line.startswith(common_prefix):
                    raw_package_path = line[len(common_prefix) :]
                    full_package_path = self.path / raw_package_path

                    package = AppPackage(path=full_package_path)
                    app_packages.append(package)

        return app_packages

    def __str__(self):
        return self.path
