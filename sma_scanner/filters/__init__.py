"""Screening conditions.

Importing this package registers every built-in filter, which is what
makes them addressable by name from config.
"""

from .base import Filter, FilterResult
from .registry import (
    FILTER_REGISTRY,
    available_filters,
    create_filter,
    filter_names,
    register,
)
# Side effect: populates FILTER_REGISTRY. Import new filter modules here.
from . import builtin  # noqa: F401

__all__ = [
    "Filter",
    "FilterResult",
    "FILTER_REGISTRY",
    "available_filters",
    "create_filter",
    "filter_names",
    "register",
]
