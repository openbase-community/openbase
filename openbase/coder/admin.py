from django.contrib import admin

from .models import ChatSession, Message

# Register your models here.
admin.site.register(ChatSession)
admin.site.register(Message)
