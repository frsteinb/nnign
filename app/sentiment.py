"""Regelbasierte Tonalität (positiv/negativ) aus Sicht des VW-Konzerns.

Gewichtete Wortstämme; geprüft wird per Präfix auf normalisierten Tokens, damit Flexionen
("Gewinne", "Gewinnsprung") erfasst werden. Bewusst einfach und nachvollziehbar; mit
API-Key übernimmt die KI die Bewertung.
"""
from __future__ import annotations

import re

from .textutil import fold

POSITIVE = {
    "gewinn": 2, "rekord": 2, "wachstum": 2, "wachs": 1, "zuwachs": 2, "plus": 1, "steiger": 1, "gestieg": 1,
    "erfolg=": 2, "erfolge=": 2, "erfolgreich": 2, "durchbruch": 2, "einigung": 1, "geeinigt": 1, "investier": 1, "investition": 1,
    "auftrag": 1, "grossauftrag": 2, "bestseller": 2, "beliebt": 1, "auszeichnung": 2, "award": 1, "testsieg": 2,
    "sieger": 1, "gewinnt": 2, "erholt": 2, "erholung": 2, "aufschwung": 2, "boom": 2, "nachfrage steigt": 2,
    "absatzplus": 2, "kursplus": 2, "aktie steigt": 2, "aktie legt zu": 2, "legt zu": 1, "zulegen": 1,
    "übertrifft": 2, "uebertrifft": 2, "besser als erwartet": 2, "optimist": 1, "zuversicht": 2,
    "neue arbeitsplätze": 2, "neue jobs": 2, "einstellungen=": 1, "partnerschaft": 1, "kooperation": 1,
    "freispruch": 2, "freigesprochen": 2, "eingestellt verfahren": 1, "vergleich erzielt": 1,
    "marktführer": 2, "spitzenreiter": 2, "vorreiter": 1, "innovation": 1, "premiere": 1, "vorgestellt": 1,
    "dividende": 1, "prognose angehoben": 3, "hebt prognose": 3, "gute zahlen": 2, "starke zahlen": 2,
    "comeback": 2, "rettet": 1, "sichert": 1, "beschäftigungssicherung": 2, "standortsicherung": 2,
    "tarifeinigung": 2, "durchgesetzt": 1, "genehmigt": 1, "zulassung erteilt": 1, "förder": 1,
}

NEGATIVE = {
    "verlust": 2, "einbruch": 2, "eingebrochen": 2, "bricht ein": 2, "rückgang": 2, "rueckgang": 2, "minus": 1,
    "sinkt": 1, "gesunken": 1, "schrumpf": 2, "krise": 2, "skandal": 3, "abgasskandal": 3, "dieselskandal": 3,
    "dieselgate": 3, "betrug": 3, "manipul": 3, "schummel": 3, "abschalteinrichtung": 2, "klage": 2, "verklagt": 2,
    "anklage": 3, "angeklagt": 3, "prozess=": 1, "prozesse=": 1, "urteil gegen": 2, "verurteil": 3, "strafe": 2, "bussgeld": 2,
    "bußgeld": 2, "schadenersatz": 2, "schadensersatz": 2, "razzia": 3, "durchsuch": 2, "ermittl": 2,
    "staatsanwalt": 2, "festgenommen": 3, "haft=": 2, "haftstrafe": 3, "rückruf": 2, "rueckruf": 2, "ruft zurück": 2, "mangel": 1,
    "defekt": 2, "brand=": 1, "brandgefahr": 2, "stellenabbau": 3, "jobabbau": 3, "abbau": 2, "streicht": 2, "stellen streichen": 3,
    "entlass": 3, "kündig": 2, "kurzarbeit": 2, "werksschliessung": 3, "werksschließung": 3, "schliesst": 2,
    "schliessung": 2, "schließung": 2, "streik": 2, "warnstreik": 2, "produktionsstopp": 3, "stillstand": 2, "lieferengpass": 2,
    "chipmangel": 2, "engpass": 2, "sparprogramm": 2, "sparkurs": 2, "kosten senken": 1, "gewinnwarnung": 3,
    "prognose gesenkt": 3, "senkt prognose": 3, "kappt": 2, "kursrutsch": 3, "aktie fällt": 2, "aktie bricht": 3,
    "absturz": 3, "schwach": 1, "enttäusch": 2, "kritik": 1, "kritisiert": 1, "vorwürfe": 2, "vorwurf": 2,
    "korruption": 3, "untreue": 3, "affäre": 3, "affaere": 3, "zwangsarbeit": 3, "menschenrecht": 2,
    "uiguren": 2, "xinjiang": 2, "zölle": 1, "zoll": 1, "strafzoll": 2, "sanktion": 1, "hackerangriff": 2,
    "datenleck": 3, "datenpanne": 3, "panne": 2, "verzöger": 2, "verschoben": 1, "probleme": 2, "problem": 1,
    "ärger": 2, "aerger": 2, "rücktritt": 1, "abgang": 1, "machtkampf": 2, "eklat": 2, "absatzminus": 2,
    "rote zahlen": 3, "milliardenverlust": 3, "insolvenz": 3, "pleite": 3, "tote=": 2, "toten=": 2, "unfall": 2,
    "tödlich": 2, "sammelklage": 3, "musterfeststellungsklage": 2, "fahrverbot": 2, "zittern": 2, "angst": 2,
    "sorge=": 1, "sorgen um": 1, "warnung": 1, "bedroht": 2, "gefährdet": 2, "drohen": 1, "droht": 1,
}

_NEGATORS = {"kein", "keine", "keinen", "nicht", "ohne", "kaum", "trotz"}

_PHRASES = [(fold(k), w) for k, w in POSITIVE.items() if " " in k] + \
           [(fold(k), -w) for k, w in NEGATIVE.items() if " " in k]
# Schlüssel mit "=" am Ende gelten nur als ganzes Wort, alle anderen als Wortanfang.
_STEMS_POS = [(fold(k), w) for k, w in POSITIVE.items() if " " not in k]
_STEMS_NEG = [(fold(k), w) for k, w in NEGATIVE.items() if " " not in k]


def _hit(word: str, stem: str) -> bool:
    if stem.endswith("="):
        return word == stem[:-1]
    return word.startswith(stem)
_WORD = re.compile(r"[a-z0-9]+")


def _score_text(text: str) -> float:
    t = fold(text)
    score = 0.0
    for phrase, w in _PHRASES:
        if phrase in t:
            score += w
    words = _WORD.findall(t)
    for i, w in enumerate(words):
        val = 0
        for st, wt in _STEMS_NEG:
            if _hit(w, st):
                val = -wt
                break
        if not val:
            for st, wt in _STEMS_POS:
                if _hit(w, st):
                    val = wt
                    break
        if val and i > 0 and words[i - 1] in _NEGATORS:
            val = -val * 0.5
        score += val
    return score


def score(title: str, summary: str = "") -> float:
    """>0 positiv, <0 negativ. Titel zählt doppelt."""
    return 2 * _score_text(title) + _score_text(summary or "")


def label(value: float, tie: str = "negativ") -> str:
    if value > 0:
        return "positiv"
    if value < 0:
        return "negativ"
    return tie
