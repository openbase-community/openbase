"""
URL configuration for openbase app using DRF ViewSets.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .viewsets import (
    AppsViewSet,
    CommandsViewSet,
    SourceCodeViewSet,
    SystemViewSet,
    TasksViewSet,
)

# Create the main router
router = DefaultRouter()
router.register(r'system', SystemViewSet, basename='system')
router.register(r'apps', AppsViewSet, basename='apps')
router.register(r'source', SourceCodeViewSet, basename='source')

# Custom paths for nested resources (tasks and commands within apps)
urlpatterns = [
    path('', include(router.urls)),
    
    # App-specific task endpoints
    path('apps/<str:app_name>/tasks/<str:task_name>/', 
         TasksViewSet.as_view({'get': 'retrieve'}), 
         name='app-task-detail'),
    
    # App-specific command endpoints  
    path('apps/<str:app_name>/commands/<str:command_name>/', 
         CommandsViewSet.as_view({'get': 'retrieve', 'delete': 'destroy'}), 
         name='app-command-detail'),
]
