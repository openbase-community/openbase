"""
URL configuration for openbase app.
"""

from django.urls import path

from . import views

urlpatterns = [
    # Management commands
    path("manage/", views.run_management_command, name="run_management_command"),
    # Debug endpoint
    path("debug/", views.debug, name="debug"),
    # Apps endpoints
    path("apps/", views.list_apps, name="list_apps"),
    path("apps/create/", views.create_app, name="create_app"),
    path("apps/<str:appname>/models/", views.get_models, name="get_models"),
    path("apps/<str:appname>/tasks/", views.get_tasks, name="get_tasks"),
    path(
        "apps/<str:appname>/tasks/<str:taskname>/",
        views.get_task_details,
        name="get_task_details",
    ),
    path("apps/<str:appname>/commands/", views.get_commands, name="get_commands"),
    path(
        "apps/<str:appname>/commands/<str:commandname>/",
        views.get_command_details,
        name="get_command_details",
    ),
    path("apps/<str:appname>/endpoints/", views.get_endpoints, name="get_endpoints"),
    path(
        "apps/<str:appname>/serializers/", views.get_serializers, name="get_serializers"
    ),
    path("apps/<str:appname>/views/", views.get_views, name="get_views"),
    path("apps/<str:appname>/api-prefix/", views.get_api_prefix, name="get_api_prefix"),
    # Settings endpoints
    path("settings/create-superuser/", views.create_superuser, name="create_superuser"),
    # Static file serving and fallback
    path("", views.serve_index, name="serve_index"),
    path(
        "<path:path>", views.serve_static_or_fallback, name="serve_static_or_fallback"
    ),
]
