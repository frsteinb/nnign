"""Erkennt, ob eine Meldung den VW-Konzern betrifft (regelbasiert, konfigurierbar)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


class _Multi:
    """Lateinische Begriffe mit Wortgrenzen, chinesische ohne (Chinesisch kennt keine Leerzeichen)."""

    def __init__(self, latin, cjk):
        self.latin, self.cjk = latin, cjk

    def findall(self, text: str) -> list:
        out = self.latin.findall(text) if self.latin else []
        return out + (self.cjk.findall(text) if self.cjk else [])

    def search(self, text: str):
        return (self.latin and self.latin.search(text)) or (self.cjk and self.cjk.search(text))


def _compile(patterns: list[str]):
    latin, cjk = [], []
    for p in patterns:
        try:
            re.compile(p)
        except re.error:
            continue
        (cjk if _CJK.search(p) else latin).append(f"(?:{p})")
    if not latin and not cjk:
        return None
    return _Multi(re.compile(r"(?<![\w-])(?:" + "|".join(latin) + r")(?!\w)") if latin else None,
                  re.compile("|".join(cjk)) if cjk else None)


@dataclass
class Matcher:
    include: _Multi | None
    exclude: _Multi | None
    min_summary_hits: int
    partners: _Multi | None = None     # nur Partner/Beteiligungen (zur Kennzeichnung)
    own: _Multi | None = None          # Konzern und Marken

    @classmethod
    def from_config(cls) -> "Matcher":
        own = list(config.get("keywords_strong"))
        if config.get("include_brands"):
            own += list(config.get("keywords_brands"))
        partners = list(config.get("keywords_partners")) if config.get("include_partners") else []
        return cls(_compile(own + partners), _compile(config.get("keywords_exclude")),
                   int(config.get("min_summary_hits")), _compile(partners) if partners else None, _compile(own))

    def scope(self, title: str, summary: str) -> str:
        """„konzern“, wenn Konzern oder Marken vorkommen, sonst „partner“ (nur Partner/Beteiligungen)."""
        text = f"{title} {summary or ''}"
        if self.own is not None and self.own.findall(text):
            return "konzern"
        if self.partners is not None and self.partners.findall(text):
            return "partner"
        return "konzern"

    def excluded(self, title: str) -> bool:
        return self.exclude is not None and bool(self.exclude.search(title or ""))

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
