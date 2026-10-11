"""Merkmale für die Themenbildung: aus Titel und Teaser wird ein gewichteter Begriffsvektor.

Ziel: Meldungen derselben Nachrichtengeschichte sollen sich ähneln, auch wenn jedes Medium anders
formuliert. Dazu
1. Wendungen vereinheitlichen („ruft … zurück“ → Rückruf, „fallen weg“ → Abbau),
2. Synonyme auf einen Begriff abbilden (Jobs/Stellen/Arbeitsplätze, zeigt/enthüllt/Weltpremiere …),
3. Komposita zerlegen („Stellenabbau“ → Stellen + Abbau, „Warnstreiks“ → Streik),
4. Modellnamen und Zahlen erkennen und stärker gewichten („ID.Tiguan“, „911“, „35.000“),
5. den Titel stärker gewichten als den Teaser.
Die Begriffslisten sind bewusst allgemein gehalten (Wirtschafts- und Autonachrichten).
"""
from __future__ import annotations

import re
from collections import Counter

from .textutil import GENERIC, STOPWORDS as _STOP, cjk_bigrams, fold, stem

# Füllwörter ohne Themenbezug (gefaltet, damit „für“ = „fur“ passt)
STOPWORDS = {fold(w) for w in _STOP} | set("""
meldet melden sieht sehen blieb bleibt hohe hoher hohen deutlich erneut weiter weitere bringt bringen zeigt sich
was wer wie warum bedeutet bedeuten erster erste ersten zweiten dritten drei vier funf zehn uber unter
""".split())

# Konzernmarken und -töchter (ohne „VW“ selbst): kennzeichnen, um wen es in der Meldung geht.
BRANDS = {"audi": "audi", "porsche": "porsche", "skoda": "skoda", "seat": "seat", "cupra": "seat",
          "bentley": "bentley", "lamborghini": "lamborghini", "bugatti": "bugatti", "ducati": "ducati",
          "traton": "traton", "scania": "traton", "navistar": "traton", "cariad": "cariad", "powerco": "powerco",
          "moia": "moia", "rivian": "rivian", "xpeng": "xpeng", "xiaopeng": "xpeng", "gotion": "gotion",
          "saic": "saic", "scout": "scout", "mobileye": "mobileye", "northvolt": "northvolt",
          "quantumscape": "quantumscape", "jac": "jac", "faw": "faw"}

# Kanonische Begriffe: Stammformen (nach fold+stem), die dasselbe meinen.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "#stellen": ("stell", "stelle", "job", "jobs", "arbeitsplatz", "arbeitsplatz", "arbeitsplatze", "arbeitsplaetz",
                 "arbeitsplätz", "beschaftigung", "personal"),
    "#abbau": ("abbau", "streich", "streicht", "gestrich", "wegfall", "kurz", "kurzung", "abgebaut", "baut_ab",
               "fallt_weg", "fall_weg", "entlass", "entlassung"),
    "#rueckruf": ("ruckruf", "zuruckgeruf", "ruft_zuruck", "ruf_zuruck", "werkstatt", "werkstatt", "werkstatten",
                  "recall"),
    "#praesentation": ("vorstell", "vorgestellt", "stellt_vor", "prasentier", "prasentiert", "zeigt", "enthull",
                       "enthullt", "premier", "premiere", "weltpremier", "debut", "debuet", "vorstellung", "zeig",
                       "gezeigt", "unveil", "check", "sitzprob", "probefahrt", "fahrbericht", "test"),
    "#gewinn": ("gewinn", "ergebnis", "verdien", "verdient", "profit", "betriebsergebnis", "rendit", "marg", "marge"),
    "#minus": ("einbruch", "bricht_ein", "brich_ein", "eingebroch", "sinkt", "sink", "gesunk", "ruckgang",
               "ruckgeh", "geht_zuruck", "weniger", "fallt", "gefall", "flaut", "schrumpf", "minus", "rucklauf",
               "senkt", "senk", "gesenkt", "kassiert", "schraubt_herunter", "herunter", "verlier", "verliert"),
    "#plus": ("steigt", "steig", "gestieg", "wachst", "wachs", "zuwachs", "plus", "rekord", "hebt", "erhoht"),
    "#prognose": ("prognos", "prognose", "renditeziel", "ausblick", "jahresziel", "ziel"),
    "#elektro": ("elektro", "elektrisch", "vollelektrisch", "e-auto", "eauto", "stromer", "elektroauto", "bev",
                 "elektrofahrzeug", "elektromobilitat", "e-mobilitat"),
    "#absatz": ("absatz", "auslieferung", "ausgeliefert", "liefert_aus", "verkauf", "verkauft", "verkauf",
                "zulassung", "neuzulassung"),
    "#chef": ("chef", "vorstandschef", "ceo", "nachfolg", "nachfolger", "fuhrungswechsel", "fuhrung", "spitz"),
    "#streik": ("streik", "warnstreik", "arbeitsniederlegung", "legen_nieder", "ausstand"),
    "#einigung": ("einig", "einigung", "geeinigt", "kompromiss", "tarifabschluss", "abschluss", "tarifeinigung"),
    "#batterie": ("batteri", "batterie", "akku", "hochvoltbatteri", "zell", "batteriemodul"),
    "#kooperation": ("kooperation", "joint", "venture", "gemeinschaftsunternehm", "allianz", "partnerschaft",
                     "deal", "einstieg", "steigt_ein"),
    "#uebernahme": ("ubernahm", "ubernahme", "ubernehm", "ubernimmt", "kauft", "ubernommen"),
    "#werk": ("werk", "standort", "fabrik", "produktionsstandort"),
    "#suv": ("suv", "gelandewag"),
    "#kleinwagen": ("kleinwag", "kleinwagen", "einstiegsmodell"),
    "#quartal": ("quartal", "quartalszahl", "quartalsbilanz", "vierteljahr", "zwischenbilanz"),
    "#gericht": ("gericht", "prozess", "urteil", "verurteilt", "landgericht", "angeklagt", "anklag"),
    "#kosten": ("kost", "kosten", "sparplan", "sparprogramm", "spar"),
    "#gewerkschaft": ("gewerkschaft", "metall", "betriebsrat"),
    "#hybrid": ("hybrid", "hybridantrieb", "t-hybrid"),
    # Spitznamen von Modellen
    "@911": ("elfer", "neunelfer"),
}
_CANON = {}
for canon, words in SYNONYMS.items():
    for w in words:
        _CANON[w] = canon

# Mehrwortwendungen (auf gefaltetem Text) -> ein Token, das dann über SYNONYMS läuft.
_PHRASES = [
    # (Verb, Partikel, Ersatz) – Wörter dazwischen bleiben erhalten („ruft 60.000 Autos zurück“)
    (r"ruf(?:t|en)", "zuruck", "ruft_zuruck"),
    (r"stell(?:t|en)", "vor", "stellt_vor"),
    (r"fall(?:t|en)", "weg", "fallt_weg"),
    (r"bau(?:t|en)", "ab", "baut_ab"),
    (r"brich(?:t)?|brechen", "ein", "bricht_ein"),
    (r"geh(?:t|en)", "zuruck", "geht_zuruck"),
    (r"lieferte?n?", "aus", "liefert_aus"),
    (r"legen", "nieder", "legen_nieder"),
    (r"steig(?:t|en)", "ein", "steigt_ein"),
    (r"schraub(?:t|en)", "herunter", "schraubt_herunter"),
]
_PHRASES = [(re.compile(rf"\b(?:{verb})\b((?:\s+[^\s:;!?]+){{0,6}}?)\s+{part}\b"), rep) for verb, part, rep in _PHRASES]
_PHRASES.append((re.compile(r"\bjoint venture\b"), "joint"))

# Bestandteile, an denen Komposita zerlegt werden (Kopf oder Bestimmungswort): nur Substantive.
_ROOTS = sorted({
    "stellen", "abbau", "ruckruf", "werkstatt", "premiere", "gewinn", "ergebnis", "profit", "einbruch", "ruckgang",
    "prognose", "elektro", "absatz", "auslieferung", "verkauf", "zulassung", "chef", "nachfolger", "streik",
    "einigung", "kompromiss", "abschluss", "batterie", "akku", "allianz", "partnerschaft", "kooperation",
    "ubernahme", "werk", "standort", "fabrik", "kleinwagen", "quartal", "gericht", "prozess", "urteil", "kosten",
    "gewerkschaft", "hybrid", "jobs", "arbeitsplatz", "arbeitsplatze",
    "airbag", "steuergerat", "tarif", "lohn", "modell", "plattform", "software", "motor", "turbo", "sportwag",
    "wagen", "auto", "fahrzeug", "beifahr", "strategie", "ziel", "frist", "zoll", "strafe", "panzer", "rustung",
    "flaute", "brand", "gefahr", "fehler", "defekt", "rendite", "umsatz", "aktie", "kurs", "betrug", "skandal",
    "abgas", "diesel", "manager", "verhandlung", "runde", "standort", "markt", "anteil", "kund", "preis",
}, key=len, reverse=True)

# Modellnamen und Kennzahlen: ID.Tiguan, ID.3, Q6, e-tron, 911, 35.000, 2,9
_WORD = re.compile(r"[A-Za-zÄÖÜäöüßÀ-ÿ]+\.[A-Za-zÄÖÜäöüß0-9]+|[A-Za-zÄÖÜäöüßÀ-ÿ0-9_]+(?:[-][A-Za-zÄÖÜäöüß0-9_]+)*"
                   r"(?:[.,][0-9]+)*")
_FUGEN = ("s", "n", "en", "es", "e")
_AFFIXES = {"haft", "lich", "bar", "los", "voll", "weise", "isch", "ung", "heit", "keit"}

# Modellnamen des Konzerns (gefaltet). Sie kennzeichnen eine Geschichte besonders stark; nennen zwei
# Meldungen verschiedene Modelle und keines gemeinsam, geht es meist um verschiedene Ereignisse.
MODELS = set("""
golf polo passat tiguan touareg t-roc t-cross taigo arteon touran sharan amarok caddy multivan transporter
id.2 id.3 id.4 id.5 id.6 id.7 id.buzz id.every1 id.2all id.tiguan id.polo id.golf id.cross id.gti up phaeton
911 718 taycan cayenne macan panamera boxster cayman
a1 a3 a4 a5 a6 a7 a8 q2 q3 q4 q5 q6 q7 q8 e-tron r8 tt
octavia fabia superb kodiaq karoq kamiq scala enyaq elroq epiq
ibiza leon arona ateca tarraco formentor born tavascan terramar
urus huracan revuelto temerario chiron tourbillon continental bentayga raval tayron elroq
""".split())
ENTITY_BOOST = 1.6   # Modellnamen, Zahlen, seltene Eigennamen
PART_WEIGHT = 0.6    # Bestandteile eines Kompositums


def _canon(w: str) -> str:
    if w in _CANON:
        return _CANON[w]
    s = stem(w)
    return _CANON.get(s, s)


def _split_compound(w: str) -> list[str]:
    """Kompositum in bekannte Bestandteile zerlegen (Kopf zuerst gesucht)."""
    if len(w) < 8 or "_" in w or w[0].isdigit():
        return []
    parts = []
    for root in _ROOTS:
        i = w.rfind(root)
        if i <= 0 and not (i == 0 and len(w) - len(root) >= 4):
            continue
        if i > 0 and i < 3:
            continue
        head_len = len(root)
        rest = w[:i] if i > 0 else w[head_len:]
        if i > 0 and rest not in _CANON and stem(rest) not in _CANON:
            for f in _FUGEN:   # Fugenlaut: Stellen|abbau, Arbeits|platz
                if rest.endswith(f) and len(rest) - len(f) >= 3:
                    rest = rest[: -len(f)]
                    break
        if len(rest) >= 3 and rest not in _AFFIXES:
            parts = [root, rest]
            break
    return parts


def _terms(text: str) -> list[tuple[str, float]]:
    text = text or ""
    out: list[tuple[str, float]] = []
    folded = " " + fold(text) + " "
    for rx, rep in _PHRASES:
        folded = rx.sub(lambda m: f" {rep}{m.group(1) if m.groups() else ''} ", folded)
    for raw in _WORD.findall(folded):
        w = raw.strip(".,-")
        if not w:
            continue
        is_model = "." in w and any(c.isalpha() for c in w) or (any(c.isdigit() for c in w)
                                                                   and any(c.isalpha() for c in w))
        is_number = w.replace(".", "").replace(",", "").isdigit()
        if is_number:
            num = w.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", w) else w
            if len(num) == 4 and num.startswith(("19", "20")):
                out.append((num, 0.5))           # Jahreszahlen sagen wenig
            elif num == "911" or num == "718":
                out.append(("@" + num, ENTITY_BOOST * 1.25))
            elif len(num) >= 2:
                out.append((num, ENTITY_BOOST))
            continue
        if is_model or w in MODELS:
            out.append(("@" + w, ENTITY_BOOST * 1.25))
            for part in re.split(r"[.\-]", w):
                if len(part) >= 3 and part not in STOPWORDS and part not in GENERIC:
                    out.append((_canon(part), ENTITY_BOOST * 0.75))
            continue
        if len(w) < 3 or w in STOPWORDS or w in GENERIC:
            continue
        for sub in w.split("-"):
            if sub in MODELS:
                out.append(("@" + sub, ENTITY_BOOST * 1.25))
                continue
            if sub in BRANDS or (sub.endswith("s") and sub[:-1] in BRANDS):
                out.append(("&" + BRANDS.get(sub, BRANDS.get(sub[:-1], sub)), 1.2))
                continue
            if len(sub) < 3 or sub in STOPWORDS or sub in GENERIC:
                continue
            c = _canon(sub)
            out.append((c, 1.0))
            if not c.startswith("#"):
                for part in _split_compound(sub):
                    pc = _canon(part)
                    if pc != c and part not in GENERIC:
                        out.append((pc, PART_WEIGHT))
    return out


SUBJECT = "!"        # Präfix für Marken/Modelle aus dem Titel (Gegenstand der Meldung), Gewicht ~0
_SUBJECT_W = 1e-6


def vector(title: str, summary: str = "") -> Counter:
    vec = Counter()
    for t, w in _terms(title):
        vec[t] += 2.0 * w
        if t[0] in "@&":
            vec[SUBJECT + t] = _SUBJECT_W
    for t, w in _terms(summary):
        vec[t] += 1.0 * w
    for t in cjk_bigrams(title):
        vec[t] += 2.0
    for t in cjk_bigrams(summary):
        vec[t] += 1.0
    return vec


def models(vec) -> set[str]:
    """Modellnamen eines Vektors (Tokens mit @). Ein Name, der nur Teil eines längeren genannten Namens
    ist („Tiguan“ neben „ID.Tiguan“), zählt nicht extra – sonst gälte der Elektro-Tiguan als Tiguan."""
    found = {t for t in vec if t.startswith("@")}
    return {t for t in found
            if not any(o != t and re.search(r"[.\-]" + re.escape(t[1:]) + r"$", o) for o in found)}


def brands(vec) -> set[str]:
    """Konzernmarken eines Vektors (Tokens mit &)."""
    return _subject(vec, "&")


def _subject(vec, prefix: str) -> set[str]:
    """Marken bzw. Modelle aus dem Titel, wenn der Titel welche nennt – sonst aus dem ganzen Text.
    (Ein Teaser erwähnt oft Schwestermodelle; worum es geht, steht im Titel.)"""
    title = {t[1:] for t in vec if t.startswith(SUBJECT + prefix)}
    return title or {t for t in vec if t.startswith(prefix)}
