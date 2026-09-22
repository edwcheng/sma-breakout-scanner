"""Filter registry - how config refers to filters by name.

Filters are looked up by string, so the scan conditions are data, not
code: change a config value and you change the screen, no edits to the
scanner required.
"""

from __future__ import annotations

from typing import Dict, List, Type

from .base import Filter

FILTER_REGISTRY: Dict[str, Type[Filter]] = {}


def register(cls: Type[Filter]) -> Type[Filter]:
    """Class decorator: `@register` on a Filter subclass makes it usable
    by name anywhere in the app."""
    if not cls.name or cls.name == "unnamed":
        raise ValueError(f"{cls.__name__} must define a unique `name`")
    if cls.name in FILTER_REGISTRY:
        raise ValueError(f"Duplicate filter name {cls.name!r}")
    FILTER_REGISTRY[cls.name] = cls
    return cls


def create_filter(name: str, **params) -> Filter:
    """Instantiate a registered filter by its config name."""
    try:
        cls = FILTER_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown filter {name!r}. Available: {sorted(FILTER_REGISTRY)}"
        ) from None
    return cls(**params)


def available_filters() -> Dict[str, str]:
    """Mapping of filter name -> description (for --list-filters)."""
    return {n: (c.description or "") for n, c in sorted(FILTER_REGISTRY.items())}


def filter_names() -> List[str]:
    return sorted(FILTER_REGISTRY)
