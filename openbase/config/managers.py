from __future__ import annotations

from rest_framework.exceptions import NotFound


class ListQuerySet:
    def __init__(self, items):
        if not isinstance(items, list):
            msg = "items must be a list"
            raise TypeError(msg)
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
        except StopIteration as e:
            msg = f"No object found with {lookup_key} == {lookup_value}"
            raise NotFound(msg) from e


class MemoryManager:
    """
    This is meant to replicate Django managers for dataclasses.
    """

    lookup_key = "name"

    def get(self, **kwargs):
        lookup_value = kwargs.pop(self.lookup_key)
        candidates = self.filter(**kwargs)
        if not isinstance(candidates, ListQuerySet):
            msg = "`filter` must return a ListQuerySet"
            raise TypeError(msg)
        result = candidates.get(self.lookup_key, lookup_value)

        return result

    def filter(self, **kwargs):
        # This method should be overridden by subclasses
        msg = "Subclasses must implement filter method"
        raise NotImplementedError(msg)

    def all(self):
        return ListQuerySet(self.filter())
