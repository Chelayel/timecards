"""Per-day changes given on the `tc fill` line, e.g. "mon pto 4, tue general +1".

Each change is DAY TARGET [HOURS]:
  DAY     mon..sun (or full name)
  TARGET  "pto", or a word identifying one card: its category, a field value
          (e.g. a subcategory), or part of the task number/title
  HOURS   N sets the hours; +N / -N adjusts the default hours for that day
          (so re-running never adds twice). Omitted for "pto" = full day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import DAYS

_DAY_ALIASES = {d[:3]: d for d in DAYS} | {d: d for d in DAYS}
_HOURS = re.compile(r"^[+-]?\d+(\.\d+)?$")


class OverrideError(ValueError):
    pass


@dataclass
class Override:
    day: str
    target: str
    hours: float | None  # None = full day (pto only)
    relative: bool  # +N / -N

    def __str__(self) -> str:
        h = "full day" if self.hours is None else (f"{self.hours:+g}" if self.relative else f"{self.hours:g}")
        return f"{self.day[:3]} {self.target} {h}"


def parse(tokens: list[str]) -> list[Override]:
    words = [w for t in tokens for w in re.split(r"[\s,;]+", t) if w]
    out: list[Override] = []
    i = 0
    while i < len(words):
        day = _DAY_ALIASES.get(words[i].lower())
        if not day:
            raise OverrideError(f"Expected a day (mon..sun), got {words[i]!r}")
        if i + 1 >= len(words):
            raise OverrideError(f"Missing card after {words[i]!r} (e.g. 'pto', 'general', a task number)")
        target = words[i + 1].lower()
        i += 2
        if i < len(words) and _HOURS.match(words[i]):
            raw = words[i]
            out.append(Override(day, target, float(raw), raw[0] in "+-"))
            i += 1
        elif target == "pto":
            out.append(Override(day, target, None, False))
        else:
            raise OverrideError(f"Missing hours for '{day[:3]} {target}' (e.g. 4, +1, -2)")
    return out
