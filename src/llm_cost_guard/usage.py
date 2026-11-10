"""Token usage for one call, and how to get it out of a response."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class UsageError(ValueError):
    """Token usage could not be determined for a call."""


class UsageWarning(UserWarning):
    """A call was recorded with zero tokens because usage could not be read."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise UsageError("token counts must not be negative")


# An extractor turns whatever the wrapped call returned into usage numbers.
Extractor = Callable[[Any], Any]

_INPUT_NAMES = ("input_tokens", "prompt_tokens")
_OUTPUT_NAMES = ("output_tokens", "completion_tokens")


def _field(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def coerce_usage(value: Any) -> Usage:
    """Accept a Usage, an ``(input, output)`` pair, or a mapping/object with
    ``input_tokens``/``output_tokens`` (or ``prompt_tokens``/``completion_tokens``)."""
    if isinstance(value, Usage):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Usage(int(value[0]), int(value[1]))
    if value is not None:
        input_tokens = _field(value, _INPUT_NAMES)
        output_tokens = _field(value, _OUTPUT_NAMES)
        if input_tokens is not None or output_tokens is not None:
            return Usage(int(input_tokens or 0), int(output_tokens or 0))
    raise UsageError(f"cannot read token usage from {type(value).__name__}")


def default_extractor(response: Any) -> Usage:
    """Look for a ``usage`` key or attribute on the response, falling back to
    token fields on the response itself."""
    nested = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    return coerce_usage(nested if nested is not None else response)
