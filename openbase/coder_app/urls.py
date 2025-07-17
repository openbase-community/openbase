"""
URL configuration for coder app using DRF ViewSets.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ClaudeCodeViewSet

# Create router for coder app
router = DefaultRouter()
router.register(r'claude', ClaudeCodeViewSet, basename='claude')

urlpatterns = [
    path('', include(router.urls)),
]
