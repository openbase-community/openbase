"""Bounded size estimates for transient container API caches."""

from __future__ import annotations

import sys
from dataclasses import fields, is_dataclass
from itertools import chain

MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024


def cache_size(value, limit: int = MAX_ENTRY_BYTES) -> int:
    """Stop at the budget; never serialize/copy a large transcript to weigh it."""
    seen = set()
    total = 0

    def visit(item, depth):
        nonlocal total
        if id(item) in seen:
            return
        seen.add(id(item))
        total += sys.getsizeof(item)
        if total > limit:
            return
        if depth > 32:
            total = limit + 1
            return
        if isinstance(item, dict):
            children = chain(item.keys(), item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            children = iter(item)
        elif is_dataclass(item) and not isinstance(item, type):
            children = (getattr(item, field.name) for field in fields(item))
        elif hasattr(item, "__dict__"):
            children = (vars(item),)
        else:
            return
        for child in children:
            visit(child, depth + 1)
            if total > limit:
                break

    visit(value, 0)
    return total
