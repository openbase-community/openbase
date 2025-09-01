import uuid
from django.db import models


class ChatSession(models.Model):
    """💬 Represents a chat session with Claude Code CLI"""
    
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    name = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Chat Session {self.public_id}"
    
    class Meta:
        ordering = ["-created_at"]


class Message(models.Model):
    """💌 Represents a message in a chat session"""
    
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    content = models.TextField()
    role = models.CharField(
        max_length=20,
        choices=[
            ("user", "User"),
            ("assistant", "Assistant"),
            ("system", "System"),
        ]
    )
    metadata = models.JSONField(default=dict, blank=True)
    claude_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Message {self.public_id} ({self.role})"
    
    class Meta:
        ordering = ["created_at"]