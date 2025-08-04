from django.core.cache import cache


class OpenbaseCache:
    """Simple cache for storing openbase objects by their unique identifiers."""

    CACHE_KEY = "openbase_objects"
    CACHE_TIMEOUT = 3600  # 1 hour

    @classmethod
    def get_object_key(cls, obj):
        """Generate a unique key for an object based on name, type, app_name, and package_name."""
        return (
            getattr(obj, "name", ""),
            obj.__class__.__name__,
            getattr(obj, "app_name", ""),
            getattr(obj, "package_name", ""),
        )

    @classmethod
    def update(cls, objects):
        """Update cache with a list of objects."""
        # Get current cache or initialize empty dict
        cached_objects = cache.get(cls.CACHE_KEY, {})

        # Update cache with new objects
        for obj in objects:
            key = cls.get_object_key(obj)
            cached_objects[key] = obj

        # Save back to cache
        cache.set(cls.CACHE_KEY, cached_objects, cls.CACHE_TIMEOUT)

    @classmethod
    def get_all(cls):
        """Get all cached objects."""
        return cache.get(cls.CACHE_KEY, {})

    @classmethod
    def clear(cls):
        """Clear the cache."""
        cache.delete(cls.CACHE_KEY)

    @classmethod
    def initialize(cls):
        """Initialize cache by loading all objects from managers."""
        from openbase.manage_commands.models import ManageCommand
        from openbase.models.models import DjangoModel
        from openbase.serializers.models import DjangoSerializer
        from openbase.tasks.models import TaskiqTask
        from openbase.urls.models import DjangoUrls
        from openbase.views.models import DjangoViewSet

        # Clear existing cache
        cls.clear()

        # List of model classes to cache
        model_classes = [
            DjangoModel,
            DjangoSerializer,
            DjangoViewSet,
            DjangoUrls,
            TaskiqTask,
            ManageCommand,
        ]

        # Load all objects from each manager
        all_objects = []
        for model_class in model_classes:
            try:
                objects = model_class.objects.all()
                all_objects.extend(
                    objects.items if hasattr(objects, "items") else objects
                )
            except Exception as e:
                print(f"Warning: Failed to load {model_class.__name__}: {e}")

        # Update cache with all objects
        cls.update(all_objects)
