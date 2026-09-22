"""The filter contract.

A filter answers one question about one symbol: pass or fail, plus the
numbers that justify the answer. Everything else in the scanner is built
around this interface, so adding a new screening condition means writing
one class and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict

from ..indicators.context import IndicatorContext


@dataclass
class FilterResult:
    """Outcome of one filter against one symbol.

    `metrics` carries whatever is worth reporting (crossover date, spread,
    volume...). The reporter renders it, so a new filter surfaces its own
    data automatically without touching the reporting code.
    """

    passed: bool
    filter_name: str
    reason: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.passed


class Filter(ABC):
    """Base class for every screening condition.

    Subclasses set `name` (the key used in config) and implement
    `evaluate`. Constructor kwargs come straight from config, so a filter
    declares its own tunable parameters.
    """

    name: str = "unnamed"
    description: str = ""

    def __init__(self, **params: Any) -> None:
        self.params = params

    @abstractmethod
    def evaluate(self, ctx: IndicatorContext) -> FilterResult:
        """Return pass/fail for this symbol.

        Raise InsufficientHistory if there is not enough data to decide -
        the scanner records that as "skipped" rather than a hard failure.
        """
        raise NotImplementedError

    # -- helpers for subclasses -----------------------------------------
    def fail(self, reason: str, **metrics: Any) -> FilterResult:
        return FilterResult(False, self.name, reason, dict(metrics))

    def ok(self, reason: str, **metrics: Any) -> FilterResult:
        return FilterResult(True, self.name, reason, dict(metrics))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        params = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"<{type(self).__name__}({params})>"
