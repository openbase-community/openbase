from rest_framework.exceptions import NotFound


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
        return candidates.get(self.lookup_key, lookup_value)
