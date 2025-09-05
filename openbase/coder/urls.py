from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'sessions', views.ChatSessionViewSet)
router.register(r'messages', views.MessageViewSet)

urlpatterns = [
    # Explicit paths should come before router.urls to take precedence
    path('send-to-claude/', views.SendToClaudeView.as_view(), name='send-to-claude'),
    path('messages/send-to-claude/', views.SendToClaudeView.as_view(), name='send-to-claude-legacy'),
    path('git/diff/', views.GitDiffView.as_view(), name='git-diff'),
    path('git/recent-commits/', views.GitRecentCommitsView.as_view(), name='git-recent-commits'),
    path('abort-claude-commands/', views.AbortClaudeCommandsView.as_view(), name='abort-claude-commands'),
    path('', include(router.urls)),
]