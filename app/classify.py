"""Entscheidungsmodelle für die Tonalität eines Themas (positiv / neutral / negativ).

Neben den Regeln und der generativen KI (llm.py) gibt es zwei reine Klassifikations- bzw.
Entscheidungsmodelle, die nur eine der drei Klassen samt Sicherheit liefern:

- lernend   – ein in nnign trainiertes Modell (Naive Bayes über das Begriffsprofil der Themen). Es lernt aus
              den Bewertungen, die der Admin von Hand gesetzt hat, und aus sicheren KI-Bewertungen. Kostenlos,
              offline, sehr schnell; wird erst genutzt, wenn genug Beispiele da sind und die per
              Kreuzvalidierung gemessene Genauigkeit reicht.
- zeroshot  – Zero-Shot-Klassifikation mit einem mehrsprachigen NLI-Modell über die Hugging-Face-Inference-API
              (kostenloses Kontingent mit Token) oder einen eigenen kompatiblen Server. Braucht kein Training:
              Das Modell prüft Aussagen wie „Diese Nachricht ist gut für den Volkswagen-Konzern.“

Welches Verfahren die Tonalität bestimmt, legt „Bewertungsverfahren“ fest (auto = KI, sonst Zero-Shot,
sonst lernend, sonst Regeln). Manuelle Bewertungen haben immer Vorrang.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from collections import Counter
from datetime import datetime, timezone

import httpx

from . import config, db, features, llm

log = logging.getLogger(__name__)

LABELS = ("positiv", "neutral", "negativ")
ORDER = ("llm", "zeroshot", "lernend", "regeln")
ORIGIN = {"llm": "llm", "zeroshot": "zeroshot", "lernend": "lernend", "regeln": "lexikon"}
NAMES = {"llm": "KI (Sprachmodell)", "zeroshot": "Zero-Shot-Klassifikation", "lernend": "Lernendes Modell",
         "regeln": "Regeln", "manuell": "Admin"}


# ---------------------------------------------------------------------------------- Auswahl

def engine() -> str:
    return config.get("sentiment_engine")


def allowed(name: str) -> bool:
    """Darf dieses Verfahren die Tonalität setzen? (Regeln immer als Rückfall)"""
    e = engine()
    return name == "regeln" or e == "auto" or e == name


def topic_text(t, articles) -> tuple[str, str]:
    """Titel und Text, die ein Klassifikator für ein Thema bekommt."""
    title = t["title"]
    body = " ".join([t["abstract"] or ""] + [a["title"] for a in articles[:6]])
    return title, body


# ---------------------------------------------------------------------------------- lernendes Modell

_model_cache: dict = {"stamp": None, "model": None}


def _doc_tokens(title: str, body: str) -> set[str]:
    return {t for t in features.vector(title, body) if not t.startswith("!")}


def training_examples() -> list[tuple[set[str], str, float]]:
    """Beispiele: manuelle Bewertungen (Gewicht 3) und sichere KI-Bewertungen (Gewicht 1)."""
    out = []
    rows = db.query("SELECT id, title, abstract, sentiment, sentiment_origin, sentiment_detail FROM topics "
                    "WHERE hidden=0 AND (sentiment_origin='manuell' OR sentiment_origin='llm')")
    for t in rows:
        weight = 3.0
        if t["sentiment_origin"] == "llm":
            try:
                conf = float(json.loads(t["sentiment_detail"] or "{}").get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < 0.6:
                continue
            weight = 1.0
        arts = db.query("SELECT title FROM articles WHERE topic_id=? ORDER BY published_at LIMIT 6", (t["id"],))
        title, body = topic_text(t, arts)
        out.append((_doc_tokens(title, body), t["sentiment"], weight))
    return out


def _train(examples) -> dict:
    df = Counter()
    for toks, _, _ in examples:
        df.update(toks)
    vocab = {t for t, n in df.items() if n >= 2} or set(df)
    counts = {c: Counter() for c in LABELS}
    totals = {c: 0.0 for c in LABELS}
    prior = {c: 0.0 for c in LABELS}
    for toks, label, w in examples:
        prior[label] += w
        for t in toks & vocab:
            counts[label][t] += w
            totals[label] += w
    return {"vocab": sorted(vocab), "counts": {c: dict(v) for c, v in counts.items()}, "totals": totals,
            "prior": prior}


def _predict(model: dict, toks: set[str]) -> tuple[str, float]:
    vocab = model.get("_vocab_set") or set(model["vocab"])
    model["_vocab_set"] = vocab
    v = len(vocab) or 1
    n_prior = sum(model["prior"].values()) or 1.0
    scores = {}
    for c in LABELS:
        if model["prior"][c] <= 0:
            continue
        s = math.log(model["prior"][c] / n_prior)
        denom = model["totals"][c] + v
        cc = model["counts"][c]
        for t in toks:
            if t in vocab:
                s += math.log((cc.get(t, 0.0) + 1.0) / denom)
        scores[c] = s
    if not scores:
        return "neutral", 0.0
    m = max(scores.values())
    exp = {c: math.exp(s - m) for c, s in scores.items()}
    z = sum(exp.values())
    best = max(exp, key=exp.get)
    return best, exp[best] / z


def train() -> dict:
    """Modell trainieren und per 5-facher Kreuzvalidierung bewerten; Ergebnis wird gespeichert."""
    ex = training_examples()
    info = {"n": len(ex), "classes": dict(Counter(e[1] for e in ex)), "trained_at": db.utcnow(), "accuracy": None}
    if len(ex) >= 10:
        rnd = random.Random(42)
        idx = list(range(len(ex)))
        rnd.shuffle(idx)
        correct = total = 0
        for k in range(5):
            test = set(idx[k::5])
            m = _train([ex[i] for i in idx if i not in test])
            for i in test:
                correct += _predict(m, ex[i][0])[0] == ex[i][1]
                total += 1
        info["accuracy"] = round(correct / total, 3) if total else None
    model = _train(ex) if ex else None
    config.state_set("learned_model", {"info": info, "model": model})
    _model_cache.update(stamp=None, model=None)
    log.info("Lernendes Modell trainiert: %s Beispiele, Genauigkeit %s", info["n"], info["accuracy"])
    return info


def learned_info() -> dict:
    data = config.state_get("learned_model") or {}
    return data.get("info") or {"n": 0, "classes": {}, "trained_at": None, "accuracy": None}


def _model():
    info = learned_info()
    if _model_cache["stamp"] != info.get("trained_at"):
        data = config.state_get("learned_model") or {}
        _model_cache.update(stamp=info.get("trained_at"), model=data.get("model"))
    return _model_cache["model"]


def learned_ready() -> bool:
    info = learned_info()
    return (info["n"] >= int(config.get("learned_min_examples")) and (info["accuracy"] or 0) >= float(
        config.get("learned_min_accuracy")) and len([c for c, n in info["classes"].items() if n >= 3]) >= 2
            and _model() is not None)


def learned_status() -> str:
    info = learned_info()
    if not info["n"]:
        return "noch keine Beispiele – lernt aus Bewertungen des Admins und sicheren KI-Bewertungen"
    acc = f"{info['accuracy'] * 100:.0f} %" if info["accuracy"] is not None else "–"
    state = "aktiv" if learned_ready() else (
        f"wartet (mind. {config.get('learned_min_examples')} Beispiele und {float(config.get('learned_min_accuracy')) * 100:.0f} % "
        "Genauigkeit nötig)")
    classes = ", ".join(f"{n} {c}" for c, n in sorted(info["classes"].items()))
    return f"{state} · {info['n']} Beispiele ({classes}) · Genauigkeit {acc} (Kreuzvalidierung)"


def predict_learned(title: str, body: str) -> tuple[str, float] | None:
    model = _model()
    if model is None:
        return None
    return _predict(model, _doc_tokens(title, body))


def maybe_retrain() -> bool:
    """Neu trainieren, wenn sich die Zahl der Beispiele spürbar geändert hat (höchstens stündlich)."""
    info = learned_info()
    n = db.query_one("SELECT COUNT(*) n FROM topics WHERE hidden=0 AND sentiment_origin IN ('manuell','llm')")["n"]
    last = info.get("trained_at")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 3600:
        return False
    if n and (not info["n"] or abs(n - info["n"]) >= max(5, info["n"] * 0.1)):
        train()
        return True
    return False


# ---------------------------------------------------------------------------------- Zero-Shot (Hugging Face)

_zs_pause = {"until": 0.0, "reason": "", "error": ""}


def zs_token() -> str:
    return (config.get("hf_token") or "").strip()


def zs_url() -> str:
    return config.get("zeroshot_url").replace("{model}", config.get("zeroshot_model").strip())


def zs_labels() -> dict[str, str]:
    """Klasse -> Formulierung für die Hypothese, aus Zeilen „positiv = gut“."""
    out = {}
    for line in config.get("zeroshot_labels"):
        k, _, v = line.partition("=")
        k = k.strip().lower()
        if k in LABELS and v.strip():
            out[k] = v.strip()
    return out if len(out) >= 2 else {"positiv": "gut", "neutral": "weder gut noch schlecht", "negativ": "schlecht"}


def zs_configured() -> bool:
    return bool(config.get("zeroshot_model").strip()) and (bool(zs_token()) or "huggingface.co" not in zs_url())


def _zs_calls_today() -> int:
    return config.state_get("zs_calls_" + datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0)


def zs_ready() -> bool:
    return zs_configured() and _zs_pause["until"] < time.time() and _zs_calls_today() < int(
        config.get("zeroshot_max_calls_per_day"))


def zs_status() -> str:
    if not zs_configured():
        return "nicht eingerichtet – HF_TOKEN in .env eintragen (kostenlos auf huggingface.co)"
    if _zs_pause["until"] > time.time():
        return f"pausiert ({_zs_pause['reason']}), danach automatisch weiter"
    if _zs_calls_today() >= int(config.get("zeroshot_max_calls_per_day")):
        return f"Tageslimit erreicht – weiter ab {llm._local(llm._next_utc_midnight())} Uhr"
    return f"aktiv: {config.get('zeroshot_model')} · heute {_zs_calls_today()} Aufrufe"


def zs_fingerprint() -> str:
    raw = "|".join([config.get("zeroshot_model"), config.get("zeroshot_hypothesis"), json.dumps(zs_labels())])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def predict_zeroshot(title: str, body: str) -> tuple[str, float, dict] | None:
    """Ein Aufruf; Rückgabe (Klasse, Sicherheit, alle Werte) oder None (nicht bereit, Limit, Fehler)."""
    if not zs_ready():
        return None
    labels = zs_labels()
    phrase_to_label = {v: k for k, v in labels.items()}
    text = (title + ". " + body)[:1500]
    body_json = {"inputs": text, "parameters": {"candidate_labels": list(labels.values()),
                                                "hypothesis_template": config.get("zeroshot_hypothesis"),
                                                "multi_label": False}}
    headers = {"Content-Type": "application/json"}
    if zs_token():
        headers["Authorization"] = f"Bearer {zs_token()}"
    day = "zs_calls_" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    config.state_set(day, config.state_get(day, 0) + 1)
    try:
        resp = httpx.post(zs_url(), json=body_json, headers=headers, timeout=float(config.get("request_timeout")) * 3)
    except httpx.HTTPError as exc:
        _zs_pause.update(until=time.time() + 300, reason="nicht erreichbar")
        log.warning("Zero-Shot nicht erreichbar: %s", exc)
        return None
    if resp.status_code in (429, 503):
        try:
            wait = float(resp.headers.get("retry-after") or (resp.json().get("estimated_time") or 60))
        except (ValueError, AttributeError):
            wait = 60.0
        _zs_pause.update(until=time.time() + min(max(wait, 20), 3600),
                         reason="Modell lädt" if resp.status_code == 503 else "Rate-Limit")
        return None
    if resp.status_code in (401, 403):
        _zs_pause.update(until=time.time() + 6 * 3600, reason="Token ungültig")
        log.error("Zero-Shot: Zugriff verweigert (HTTP %s) – HF_TOKEN prüfen", resp.status_code)
        return None
    if resp.status_code in (404, 410):
        _zs_pause.update(until=time.time() + 6 * 3600,
                         reason="Modell dort nicht verfügbar – unter Einstellungen ein anderes wählen")
        log.error("Zero-Shot: Modell %s nicht verfügbar (HTTP %s)", config.get("zeroshot_model"), resp.status_code)
        return None
    if resp.status_code >= 400:
        log.warning("Zero-Shot: HTTP %s %s", resp.status_code, resp.text[:200])
        _zs_pause["error"] = f"HTTP {resp.status_code}: {resp.text[:150]}"
        if resp.status_code == 402:
            _zs_pause.update(until=time.time() + 6 * 3600, reason="kostenloses Kontingent aufgebraucht")
        return None
    try:
        data = resp.json()
        if isinstance(data, list) and data and isinstance(data[0], list):
            data = data[0]
        if isinstance(data, dict):
            pairs = list(zip(data["labels"], data["scores"]))
        else:
            pairs = [(d["label"], d["score"]) for d in data]
    except (ValueError, KeyError, TypeError, IndexError):
        log.warning("Zero-Shot: Antwort nicht lesbar: %r", resp.text[:200])
        return None
    scores = {phrase_to_label.get(lbl, lbl): float(sc) for lbl, sc in pairs if phrase_to_label.get(lbl, lbl) in LABELS}
    if not scores:
        return None
    config.state_set("zs_last", db.utcnow())
    _zs_pause["error"] = ""
    best = max(scores, key=scores.get)
    return best, scores[best], scores


def test_zeroshot() -> str:
    if not zs_configured():
        return zs_status()
    r = predict_zeroshot("Volkswagen ruft 50.000 Autos wegen Airbag-Problemen zurück", "")
    if r is None:
        return "Fehlgeschlagen – " + zs_status()
    return f"OK: „Rückruf“ → {r[0]} ({r[1] * 100:.0f} %)"


# ---------------------------------------------------------------------------------- Vergleich

def compare(title: str, body: str, use_remote: bool = True) -> list[dict]:
    """Alle Verfahren auf einen Text anwenden (Test im Admin-Bereich)."""
    from . import llm, sentiment
    out = []
    res = sentiment.analyze(title, body)
    out.append({"engine": "regeln", "label": sentiment.label_for(res), "confidence": None,
                "detail": f"+{res.pos:g} / −{res.neg:g}"})
    p = predict_learned(title, body)
    out.append({"engine": "lernend", "label": p[0] if p else None, "confidence": p[1] if p else None,
                "detail": learned_status()})
    if use_remote:
        z = predict_zeroshot(title, body)
        out.append({"engine": "zeroshot", "label": z[0] if z else None, "confidence": z[1] if z else None,
                    "detail": ", ".join(f"{k} {v * 100:.0f} %" for k, v in z[2].items()) if z else zs_status()})
        r = llm.analyze({"title": title, "summary": body, "source": "Test", "published": ""}, []) \
            if llm.ready() else None
        out.append({"engine": "llm", "label": r["sentiment"] if r else None,
                    "confidence": r.get("confidence") if r else None,
                    "detail": (r.get("begruendung") if r else None) or llm.describe()})
    return out
