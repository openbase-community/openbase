from rest_framework.exceptions import NotFound

from openbase.openbase_app.cache import OpenbaseCache


class ListQuerySet:
    def __init__(self, items):
        assert isinstance(items, list), "items must be a list"
        self.items = items

    def __iter__(self):
        return iter(self.items)

    def get(self, lookup_key, lookup_value):
        try:
            return next(
                candidate
                for candidate in self.items
                if getattr(candidate, lookup_key) == lookup_value
            )
        except StopIteration:
            raise NotFound(
                f"No {self.model.__name__} found with {lookup_key} == {lookup_value}"
            )


class MemoryManager:
    """
    This is meant to replicate Django managers for dataclasses.
    """

    lookup_key = "name"

    def get(self, **kwargs):
        lookup_value = kwargs.pop(self.lookup_key)
        candidates = self.filter(**kwargs)
        assert isinstance(candidates, ListQuerySet), (
            "`filter` must return a ListQuerySet"
        )
        result = candidates.get(self.lookup_key, lookup_value)

        # Update cache with the single result
        OpenbaseCache.update([result])

        return result

    def filter(self, **kwargs):
        # This method should be overridden by subclasses
        raise NotImplementedError("Subclasses must implement filter method")

    def all(self):
        return ListQuerySet(self.filter())
