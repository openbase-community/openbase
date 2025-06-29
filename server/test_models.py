from django.db import models


class User(models.Model):
    """User model for the application."""

    name = models.CharField(max_length=100)
    email = models.EmailField()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)


class Profile(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    bio = models.TextField()
