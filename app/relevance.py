"""Erkennt, ob eine Meldung den VW-Konzern betrifft (regelbasiert, konfigurierbar)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config


def _compile(patterns: list[str]) -> re.Pattern | None:
    good = []
    for p in patterns:
        try:
            re.compile(p)
            good.append(f"(?:{p})")
        except re.error:
            continue
    if not good:
        return None
    return re.compile(r"(?<![\w-])(?:" + "|".join(good) + r")(?!\w)")


@dataclass
class Matcher:
    include: re.Pattern | None
    exclude: re.Pattern | None
    min_summary_hits: int

    @classmethod
    def from_config(cls) -> "Matcher":
        inc = list(config.get("keywords_strong"))
        if config.get("include_brands"):
            inc += list(config.get("keywords_brands"))
        return cls(_compile(inc), _compile(config.get("keywords_exclude")), int(config.get("min_summary_hits")))

    def score(self, title: str, summary: str) -> int:
        """0 = irrelevant; >0 = Anzahl Treffer (Titeltreffer zählen dreifach)."""
        if self.include is None:
            return 0
        if self.exclude is not None and self.exclude.search(title):
            return 0
        t = len(self.include.findall(title))
        s = len(self.include.findall(summary or ""))
        if t:
            return 3 * t + s
        return s if s >= self.min_summary_hits else 0
