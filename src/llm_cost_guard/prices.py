"""A user-supplied price table. No prices are built in: model prices change
and this library does not know which models you use."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

TOKENS_PER_UNIT = 1_000_000  # prices are quoted in USD per million tokens


class UnknownModelError(KeyError):
    """A call named a model that has no entry in the price table."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class PriceTable:
    """Maps a model name to its input and output price in USD per 1M tokens.

    >>> table = PriceTable({"small-model": {"input": 0.50, "output": 1.50}})
    >>> table.cost("small-model", input_tokens=1000, output_tokens=2000)
    0.0035
    """

    def __init__(
        self,
        prices: Mapping[str, Mapping[str, float]],
        default: Mapping[str, float] | None = None,
    ) -> None:
        self._prices = {name: self._validate(name, entry) for name, entry in prices.items()}
        self._default = self._validate("default", default) if default is not None else None

    @staticmethod
    def _validate(name: str, entry: Mapping[str, float]) -> tuple[float, float]:
        try:
            rates = float(entry["input"]), float(entry["output"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"price for '{name}' must look like {{'input': 0.5, 'output': 1.5}}"
            ) from exc
        if min(rates) < 0:
            raise ValueError(f"price for '{name}' must not be negative")
        return rates

    @classmethod
    def from_file(cls, path: str | Path) -> "PriceTable":
        """Load ``{"models": {name: {"input": x, "output": y}}, "default": {...}}``.
        ``default`` is optional. A flat ``{name: {...}}`` mapping also works."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        if "models" in data:
            return cls(data["models"], data.get("default"))
        return cls(data)

    def knows(self, model: str) -> bool:
        return model in self._prices or self._default is not None

    def rates(self, model: str) -> tuple[float, float]:
        if model in self._prices:
            return self._prices[model]
        if self._default is not None:
            return self._default
        raise UnknownModelError(
            f"no price for model '{model}'; add it to the price table or set a default price"
        )

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        input_rate, output_rate = self.rates(model)
        return (input_tokens * input_rate + output_tokens * output_rate) / TOKENS_PER_UNIT
