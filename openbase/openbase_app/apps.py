from django.apps import AppConfig


class OpenbaseAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "openbase.openbase_app"
    
    def ready(self):
        """Initialize the cache when the app is ready."""
        from openbase.openbase_app.cache import OpenbaseCache
        
        # Initialize cache with all objects
        OpenbaseCache.initialize()
