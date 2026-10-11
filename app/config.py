"""Konfigurations-Registry.

Rangfolge: im Admin-UI gespeicherter Wert (DB) > Umgebungsvariable NNIGN_<KEY> > Default.
Alle Einstellungen sind im Admin-Bereich unter /admin editierbar.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from . import db


@dataclass
class Setting:
    key: str
    type: str  # int | float | bool | str | text | list | secret | choice
    default: Any
    label: str
    help: str = ""
    group: str = "Allgemein"
    choices: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)  # zusätzliche Umgebungsvariablen (Aliase)


DEFAULT_STRONG = [
    r"Volkswagen\w*",
    r"VW",
    r"VWs",
    r"VW-\w+",
    r"Wolfsburger (?:Autobauer|Konzern|Autokonzern)",
    r"Oliver Blume",
    r"Herbert Diess",
    r"Martin Winterkorn",
    r"Matthias Müller",
    r"Ferdinand Piëch",
    r"Bernd Pischetsrieder",
    r"Porsche SE",
    r"Cariad",
    r"PowerCo",
    r"Traton",
    r"Moia",
    r"Dieselskandal",
    r"Abgasskandal",
    r"Dieselgate",
]

DEFAULT_BRANDS = [
    r"Audi\w*",
    r"Porsche\w*",
    r"Škoda|Skoda",
    r"Seat",
    r"Cupra",
    r"Bentley",
    r"Lamborghini",
    r"Bugatti",
    r"Ducati",
    r"Scania",
    r"MAN Truck\w*",
    r"MAN-Lkw",
    r"Navistar",
]

DEFAULT_EXCLUDE = [
    r"VfL Wolfsburg",
    r"Bundesliga",
    r"Volkswagen Arena",
    r"Audi Dome",
    r"Porsche Arena",
    r"Ferdinand Porsche Gymnasium",
]

SETTINGS: list[Setting] = [
    # --- Zeitplan
    Setting("scan_interval_hours", "float", 4.0, "Scan-Intervall (Stunden)",
            "Abstand zwischen zwei regulären Scans der aktuellen Feeds.", "Zeitplan"),
    Setting("initial_recent_days", "int", 60, "Initial-Scan: Suchfenster (Tage)",
            "Der Initial-Scan fragt die Nachrichtensuche für die letzten N Tage (höchstens bis zum Beginn des "
            "Erfassungszeitraums) in Wochenfenstern ab; ältere Zeiträume übernimmt der Backfill.", "Zeitplan"),
    Setting("regular_lookback_days", "int", 3, "Regulärer Scan: max. Alter (Tage)",
            "Ältere Artikel aus Feeds werden bei regulären Scans ignoriert (der Archiv-Backfill ist davon unabhängig).", "Zeitplan"),
    # --- Historischer Backfill
    Setting("backfill_enabled", "bool", True, "Historischer Backfill aktiv",
            "Arbeitet im Hintergrund Archivseiten, archivierte Feeds und Suchzeitfenster ab.", "Archiv / Backfill"),
    Setting("collect_since", "str", "", "Erfassung ab (JJJJ-MM-TT)",
            "Beginn des Erfassungszeitraums. Leer = Standard vom Erststart (vier Wochen vor dem ersten Start). "
            "Ändern am besten über Admin → Daten & Zeitraum.", "Erfassungszeitraum"),
    Setting("collect_until", "str", "", "Erfassung bis (JJJJ-MM-TT, leer = laufend)",
            "Ende des Erfassungszeitraums; leer = bis heute und laufend weiter.", "Erfassungszeitraum"),
    Setting("backfill_wayback", "bool", True, "Internet Archive (Wayback) für Feeds nutzen",
            "Holt archivierte Schnappschüsse der RSS-Feeds aus dem Internet Archive.", "Archiv / Backfill"),
    Setting("backfill_wayback_granularity", "choice", "month", "Wayback-Schnappschüsse pro",
            "Ein Schnappschuss je Tag/Woche/Monat und Feed.", "Archiv / Backfill", ["day", "week", "month"]),
    Setting("backfill_wayback_fallback", "bool", True, "Archivseiten notfalls über Wayback laden",
            "Wenn eine Archivseite direkt nicht erreichbar ist, wird der nächstgelegene Wayback-Schnappschuss versucht.",
            "Archiv / Backfill"),
    Setting("backfill_search_window_days", "int", 90, "Suchzeitfenster (Tage)",
            "Die Nachrichtensuche liefert max. ~100 Treffer je Anfrage; der Zeitraum wird daher in Fenster dieser Länge zerlegt.",
            "Archiv / Backfill"),
    Setting("backfill_fetch_details", "bool", True, "Details zu Archiv-Treffern laden",
            "Lädt für relevante Archiv-Treffer die Artikelseite und liest Beschreibung/Datum aus den Meta-Tags.",
            "Archiv / Backfill"),
    Setting("backfill_workers", "int", 4, "Parallele Abrufe", "Anzahl paralleler Downloads (verschiedene Hosts).",
            "Archiv / Backfill"),
    Setting("backfill_batch", "int", 24, "Aufgaben je Durchgang", "", "Archiv / Backfill"),
    # --- Abruf
    Setting("search_enabled", "bool", True, "Google-News-Suche verwenden",
            "Ergänzt die RSS-Feeds um eine Suche mit site:<domain> (findet auch Artikel, die nicht im Feed stehen).",
            "Abruf"),
    Setting("search_query", "str", "Volkswagen OR VW OR Audi OR Porsche OR Skoda OR Seat OR Cupra OR Traton OR Cariad",
            "Suchanfrage", "Wird um site:<domain> und Zeitfenster ergänzt.", "Abruf"),
    Setting("host_delay_seconds", "float", 1.2, "Pause je Host (Sekunden)",
            "Mindestabstand zwischen zwei Anfragen an denselben Host (Höflichkeit/Ratelimits).", "Abruf"),
    Setting("wayback_delay_seconds", "float", 4.0, "Pause je Anfrage an das Internet Archive (Sekunden)",
            "Das Internet Archive drosselt schnelle Abrufe; daher ein eigener, größerer Abstand.", "Abruf"),
    Setting("request_timeout", "int", 25, "HTTP-Timeout (Sekunden)", "", "Abruf"),
    Setting("user_agent", "str",
            "Mozilla/5.0 (compatible; nnign/1.0; +https://github.com/frsteinb/nnign)",
            "User-Agent", "", "Abruf"),
    # --- Relevanz
    Setting("keywords_strong", "list", DEFAULT_STRONG, "Konzern-Begriffe (Regex, eine pro Zeile)",
            "Groß-/Kleinschreibung wird beachtet, Wortgrenzen werden automatisch ergänzt.", "Relevanz"),
    Setting("keywords_brands", "list", DEFAULT_BRANDS, "Marken-Begriffe (Regex, eine pro Zeile)",
            "Konzernmarken; zählen ebenfalls als Treffer, wenn 'Marken einbeziehen' aktiv ist.", "Relevanz"),
    Setting("include_brands", "bool", True, "Marken einbeziehen", "", "Relevanz"),
    Setting("keywords_exclude", "list", DEFAULT_EXCLUDE, "Ausschluss-Begriffe (Regex)",
            "Artikel, deren Titel einen dieser Begriffe enthält, werden verworfen.", "Relevanz"),
    Setting("min_summary_hits", "int", 2, "Min. Treffer im Teaser (ohne Titeltreffer)",
            "Steht kein Begriff im Titel, müssen so viele Treffer im Teasertext vorkommen.", "Relevanz"),
    # --- Themenbildung
    Setting("cluster_threshold", "float", 0.34, "Ähnlichkeitsschwelle Thema",
            "Ab dieser Kosinus-Ähnlichkeit (0..1) wird ein Artikel einem bestehenden Thema zugeordnet.", "Themen"),
    Setting("cluster_window_days", "int", 6, "Themen-Zeitfenster (Tage)",
            "Ein Artikel kann nur Themen zugeordnet werden, deren letzte Meldung höchstens so lange zurückliegt.", "Themen"),
    Setting("max_sources_per_topic", "int", 5, "Max. angezeigte Quellen je Thema", "", "Themen"),
    # --- Bewertung
    Setting("sentiment_neutral_margin", "float", 1.5, "Neutral-Marge",
            "Ist der Saldo aus positiven und negativen Signalen kleiner als dieser Wert, gilt die Meldung als "
            "neutral. Höher = mehr neutrale Bewertungen.", "Bewertung"),
    Setting("sentiment_mixed_ratio", "float", 0.75, "Gemischt-Schwelle",
            "Erreicht die schwächere Seite mindestens diesen Anteil der stärkeren (0..1), gilt die Meldung "
            "als gemischt und damit neutral.", "Bewertung"),
    Setting("lexicon_extra", "list", [], "Eigene Bewertungsbegriffe",
            "Ein Begriff je Zeile im Format „begriff = gewicht“, z. B. „sondermodell = 1.5“ oder "
            "„lieferstopp = -3“. Der Begriff zählt als Wortanfang; positives Gewicht = positiv. "
            "Danach „Bewertungen neu berechnen“.", "Bewertung"),
    # --- KI
    Setting("llm_enabled", "bool", True, "KI-Analyse verwenden",
            "Nur aktiv, wenn Anbieter, Modell und ggf. API-Key gesetzt sind. Sonst arbeitet das System "
            "rein regelbasiert.", "KI"),
    Setting("llm_provider", "choice", "anthropic", "KI-Anbieter",
            "anthropic = Claude (offizielles SDK); alle anderen über die OpenAI-kompatible Chat-Completions-"
            "Schnittstelle. openai_compatible = beliebiger kompatibler Server (LM Studio, vLLM, LiteLLM …) "
            "über die Basis-URL. Umgebungsvariable: LLM_PROVIDER.", "KI",
            ["anthropic", "openai", "azure", "gemini", "mistral", "groq", "openrouter", "ollama",
             "openai_compatible"], env=["LLM_PROVIDER"]),
    Setting("llm_model", "str", "", "Modell",
            "Leer = Standard des Anbieters (nur bei Anthropic vorhanden: claude-opus-5-5; günstiger: "
            "claude-haiku-5-5). Bei allen anderen Anbietern Pflicht, bei Azure der Deployment-Name. "
            "Umgebungsvariable: LLM_MODEL.", "KI", env=["LLM_MODEL"]),
    Setting("llm_api_key", "secret", "", "API-Key",
            "Leer = anbieterspezifische Umgebungsvariable (ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "AZURE_OPENAI_API_KEY, GEMINI_API_KEY, MISTRAL_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY). "
            "Ollama braucht keinen Key. Umgebungsvariable: LLM_API_KEY.", "KI", env=["LLM_API_KEY"]),
    Setting("llm_base_url", "str", "", "Basis-URL",
            "Leer = Standard des Anbieters. Bei Azure der Endpoint (https://<name>.openai.azure.com, "
            "alternativ AZURE_OPENAI_ENDPOINT), bei Ollama z. B. http://host.docker.internal:11434/v1. "
            "Umgebungsvariable: LLM_BASE_URL.", "KI", env=["LLM_BASE_URL"]),
    Setting("azure_api_version", "str", "2024-10-21", "Azure: API-Version", "", "KI",
            env=["AZURE_OPENAI_API_VERSION"]),
    Setting("anthropic_api_key", "secret", "", "Anthropic API-Key (alt)",
            "Wird verwendet, wenn beim Anbieter anthropic kein allgemeiner API-Key gesetzt ist. "
            "Alternativ Umgebungsvariable ANTHROPIC_API_KEY.", "KI", env=["ANTHROPIC_API_KEY"]),
    Setting("llm_system_prompt", "text", "", "System-Prompt",
            "Rolle und Grundregeln für das Modell. Leer = Standard (siehe Dokumentation). "
            "„auf Standard“ stellt den mitgelieferten Text wieder her.", "KI"),
    Setting("llm_instructions", "text", "", "Arbeitsanweisung (Prompt)",
            "Aufgaben und Bewertungskriterien. Leer = Standard. Die Daten der Meldung (Titel, Teaser, Quelle, "
            "Datum, Kandidaten-Themen) werden automatisch angehängt; das Antwortformat ist fest vorgegeben.", "KI"),
    Setting("llm_effort", "choice", "low", "Denkaufwand (effort, nur Anthropic)", "", "KI",
            ["low", "medium", "high"]),
    Setting("llm_for_backfill", "bool", False, "KI auch für historischen Backfill",
            "Achtung: bei 25 Jahren Historie viele tausend Aufrufe.", "KI"),
    Setting("llm_max_calls_per_day", "int", 1500, "Max. KI-Aufrufe pro Tag",
            "Kostenbremse; danach regelbasiert.", "KI"),
    # --- Anzeige
    Setting("public_dashboard", "bool", True, "Dashboard ohne Login sichtbar",
            "Wenn aus, ist auch die Übersicht passwortgeschützt.", "Anzeige"),
    Setting("timezone", "str", "Europe/Berlin", "Zeitzone für die Anzeige", "", "Anzeige"),
    Setting("page_size", "int", 50, "Themen pro Seite", "", "Anzeige"),
]

BY_KEY = {s.key: s for s in SETTINGS}


def _env_value(s: Setting):
    """Wert aus NNIGN_<KEY> oder einem Alias; leere Variablen gelten als nicht gesetzt."""
    for name in ["NNIGN_" + s.key.upper(), *s.env]:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return parse_value(s, raw)
    return None


def parse_value(s: Setting, raw: Any):
    if s.type == "int":
        return int(float(raw))
    if s.type == "float":
        return float(str(raw).replace(",", "."))
    if s.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "ja", "on")
    if s.type == "list":
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [line.strip() for line in str(raw).splitlines() if line.strip()]
    if s.type == "choice":
        v = str(raw).strip()
        return v if v in s.choices else s.default
    return str(raw).strip()


def get(key: str):
    s = BY_KEY[key]
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is not None:
        try:
            return parse_value(s, json.loads(row["value"]))
        except (ValueError, TypeError):
            pass
    env = _env_value(s)
    if env is not None:
        return env
    return s.default


def source_of(key: str) -> str:
    if db.query_one("SELECT 1 FROM settings WHERE key=?", (key,)):
        return "admin"
    return "env" if _env_value(BY_KEY[key]) is not None else "default"


def set(key: str, value) -> None:  # noqa: A001 - bewusst kurz
    s = BY_KEY[key]
    value = parse_value(s, value)
    with db.tx() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def reset(key: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


# --- interne Zustandswerte (nicht im Admin-Formular)

def state_get(key: str, default=None):
    row = db.query_one("SELECT value FROM settings WHERE key=?", ("_" + key,))
    return json.loads(row["value"]) if row else default


def state_set(key: str, value) -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("_" + key, json.dumps(value)))
