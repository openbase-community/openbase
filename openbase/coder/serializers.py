from rest_framework import serializers
from .models import ChatSession, Message


class ChatSessionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source='public_id', read_only=True)
    
    class Meta:
        model = ChatSession
        fields = ['id', 'name', 'metadata', 'created_at', 'updated_at']


class MessageSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source='public_id', read_only=True)
    session_id = serializers.UUIDField(source='session.public_id', read_only=True)
    
    class Meta:
        model = Message
        fields = ['id', 'session_id', 'content', 'role', 'metadata', 'claude_response', 'created_at']


class MessageCreateSerializer(serializers.ModelSerializer):
    session_id = serializers.UUIDField(write_only=True)
    
    class Meta:
        model = Message
        fields = ['session_id', 'content', 'role', 'metadata']
    
    def create(self, validated_data):
        session_id = validated_data.pop('session_id')
        session = ChatSession.objects.get(public_id=session_id)
        validated_data['session'] = session
        return super().create(validated_data)