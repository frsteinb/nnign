"""Übersetzung fremdsprachiger Meldungen ins Deutsche.

Anbieter (Einstellung „Übersetzung“):
  libretranslate – freier, selbst betriebener Übersetzungsserver (kein API-Key nötig), z. B. als zweiter
                   Container über docker compose --profile uebersetzung
  deepl          – DeepL-API (Key nötig, kostenloser Tarif verfügbar)
  llm            – der konfigurierte KI-Anbieter (auch lokal per Ollama, ohne Key)
  auto           – erster verfügbarer der obigen in dieser Reihenfolge
  none           – nicht übersetzen; Texte bleiben im Original und werden gekennzeichnet
Titel und Teaser werden beim Speichern übersetzt; das Original bleibt erhalten.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import config, llm

log = logging.getLogger(__name__)

NAMES = {"libretranslate": "LibreTranslate", "deepl": "DeepL", "llm": "KI", "": ""}

# Bei Limits (HTTP 429, DeepL 456 = Kontingent erschöpft) kurz pausieren und danach selbst weitermachen
_pause = {"until": 0.0, "reason": ""}


def _limit(resp, name: str) -> bool:
    """True, wenn die Antwort ein Limit meldet (dann pausieren)."""
    if resp.status_code == 456:
        _pause.update(until=time.time() + 6 * 3600, reason=f"{name}: Kontingent erschöpft")
    elif resp.status_code == 429:
        try:
            secs = float(resp.headers.get("retry-after") or 600)
        except ValueError:
            secs = 600.0
        _pause.update(until=time.time() + secs, reason=f"{name}: Rate-Limit")
    else:
        return False
    log.warning("Übersetzung pausiert: %s", _pause["reason"])
    return True


def paused_reason() -> str:
    return _pause["reason"] if _pause["until"] > time.time() else ""


def provider() -> str:
    """Tatsächlich genutzter Anbieter ('' = keiner verfügbar)."""
    choice = config.get("translate_provider")
    if choice == "none":
        return ""
    available = {
        "libretranslate": bool((config.get("libretranslate_url") or "").strip()),
        "deepl": bool((config.get("deepl_api_key") or "").strip()),
        "llm": llm.available(),
    }
    if choice in available:
        return choice if available[choice] else ""
    for name in ("libretranslate", "deepl", "llm"):
        if available[name]:
            return name
    return ""


def describe() -> str:
    p = provider()
    if p and paused_reason():
        return f"aktiv: {NAMES[p]} – pausiert ({paused_reason()}), danach automatisch weiter"
    if p:
        return f"aktiv: {NAMES[p]}"
    if config.get("translate_provider") == "none":
        return "abgeschaltet – fremdsprachige Texte bleiben im Original"
    return "kein Übersetzer verfügbar – fremdsprachige Texte bleiben im Original (siehe Doku)"


def _timeout() -> float:
    return float(config.get("request_timeout")) * 2


def _libretranslate(texts: list[str], lang: str) -> list[str] | None:
    url = config.get("libretranslate_url").strip().rstrip("/") + "/translate"
    key = (config.get("libretranslate_api_key") or "").strip()
    # Chinesisch heißt je nach LibreTranslate-Version „zh“ oder „zh-Hans“; unbekannter Code -> automatisch
    for source in {"en": ["en", "auto"], "zh": ["zh-Hans", "zh", "auto"]}.get(lang, ["auto"]):
        body = {"q": texts, "source": source, "target": "de", "format": "text"}
        if key:
            body["api_key"] = key
        try:
            resp = httpx.post(url, json=body, timeout=_timeout())
        except httpx.HTTPError as exc:
            log.warning("LibreTranslate nicht erreichbar: %s", exc)
            return None
        if resp.status_code == 400 and source != "auto":
            continue  # Sprachcode unbekannt -> automatische Erkennung
        if _limit(resp, "LibreTranslate"):
            return None
        if resp.status_code >= 400:
            log.warning("LibreTranslate: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        out = resp.json().get("translatedText")
        if isinstance(out, str) and len(texts) == 1:
            out = [out]
        if isinstance(out, list) and len(out) == len(texts):
            return [str(x) for x in out]
        log.warning("LibreTranslate: unerwartete Antwort %r", resp.text[:200])
        return None
    return None


def _deepl(texts: list[str], lang: str) -> list[str] | None:
    key = config.get("deepl_api_key").strip()
    url = (config.get("deepl_url") or "").strip() or (
        "https://api-free.deepl.com/v2/translate" if key.endswith(":fx") else "https://api.deepl.com/v2/translate")
    body = {"text": texts, "target_lang": "DE"}
    if lang in ("en", "zh"):
        body["source_lang"] = lang.upper()
    try:
        resp = httpx.post(url, json=body, headers={"Authorization": f"DeepL-Auth-Key {key}"}, timeout=_timeout())
    except httpx.HTTPError as exc:
        log.warning("DeepL nicht erreichbar: %s", exc)
        return None
    if _limit(resp, "DeepL"):
        return None
    if resp.status_code >= 400:
        log.warning("DeepL: HTTP %s %s", resp.status_code, resp.text[:200])
        return None
    out = [t.get("text", "") for t in resp.json().get("translations", [])]
    return out if len(out) == len(texts) else None


def translate_texts(texts: list[str], lang: str) -> tuple[list[str] | None, str]:
    """Übersetzt Texte aus `lang` ins Deutsche. Rückgabe: (Übersetzungen oder None, Anbieter)."""
    p = provider()
    if not p or not texts or paused_reason():
        return None, ""
    out = {"libretranslate": _libretranslate, "deepl": _deepl, "llm": llm.translate}[p](texts, lang)
    return out, (p if out is not None else "")


def translate_items(items: list, batch: int = 40, allow_llm: bool = True) -> int:
    """Übersetzt Titel und Teaser nicht-deutscher Meldungen (in Gruppen je Sprache).
    Originale wandern nach title_orig/summary_orig. Gibt die Zahl übersetzter Meldungen zurück.
    allow_llm=False: KI-Übersetzung auslassen (Backfill ohne KI-Freigabe)."""
    if provider() == "llm" and not allow_llm:
        return 0
    done = 0
    for lang in sorted({it.lang for it in items if it.lang != "de"}):
        group = [it for it in items if it.lang == lang and not it.translated_by]
        for i in range(0, len(group), batch):
            chunk = group[i:i + batch]
            texts, slots = [], []
            for it in chunk:
                for field in ("title", "summary"):
                    value = getattr(it, field)
                    if value:
                        slots.append((it, field))
                        texts.append(value)
            result, used = translate_texts(texts, lang)
            if result is None:
                continue
            for (it, field), text in zip(slots, result):
                setattr(it, field + "_orig", getattr(it, field))
                setattr(it, field, text.strip() or getattr(it, field))
            for it in chunk:
                it.translated_by = used
                done += 1
    return done


def test_connection() -> str:
    p = provider()
    if not p:
        return describe()
    out, used = translate_texts(["Volkswagen recalls 50,000 cars over airbag problems"], "en")
    return f"OK ({NAMES[used]}): {out[0]}" if out else f"Fehlgeschlagen ({NAMES[p]}) – Details im Container-Log."
