from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'sessions', views.ChatSessionViewSet)
router.register(r'messages', views.MessageViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('git/diff/', views.GitDiffView.as_view(), name='git-diff'),
    path('git/recent-commits/', views.GitRecentCommitsView.as_view(), name='git-recent-commits'),
]