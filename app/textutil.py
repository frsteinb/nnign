"""Text-Hilfen: HTML säubern, Tokenisierung, einfache deutsche Stammformbildung."""
from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]+(?:[.,][0-9]+)?")

STOPWORDS = set("""
aber alle allem allen aller alles als also am an ander andere anderem anderen anderer anderes anderm andern anderr
anders auch auf aus bei beim bin bis bist da damit dann das dass daß dein deine dem den der des dessen deshalb die
dies diese diesem diesen dieser dieses doch dort du durch ein eine einem einen einer eines einig einige einigem
einigen einiger einiges einmal er es etwas euer eure für gegen gewesen hab habe haben hat hatte hatten hier hin
hinter ich ihm ihn ihnen ihr ihre ihrem ihren ihrer ihres im in indem ins ist jede jedem jeden jeder jedes jene
jenem jenen jener jenes jetzt kann kein keine keinem keinen keiner keines können könnte machen man manche manchem
manchen mancher manches mein meine mehr mit muss musste nach nicht nichts noch nun nur ob oder ohne sehr sein
seine seinem seinen seiner seines selbst sich sie sind so solche solchem solchen solcher solches soll sollte
sondern sonst über um und uns unsere unserem unseren unser unseres unter viel vom von vor während war waren warst
was weg weil weiter welche welchem welchen welcher welches wenn werde werden wie wieder will wir wird wirst wo
wollen wollte würde würden zu zum zur zwar zwischen neue neuen neuer neues erst ersten jahr jahre jahren prozent
mehr laut sagt sagte heute gestern morgen wochen woche monat monate millionen milliarden mio mrd euro dollar
seit wegen bereits rund etwa schon immer kommt gibt geht steht soll sollen könnten will wollen hätte hätten
dpa reuters afp video audio liveblog news ticker newsblog spiegel zeit faz tagesschau handelsblatt online
the and for with from
""".split())

# Begriffe, die (fast) jede Meldung enthält und daher nichts über das Thema aussagen.
GENERIC = set("""
vw volkswagen volkswagens konzern konzerns autobauer autobauers autokonzern autokonzerns wolfsburg wolfsburger
unternehmen hersteller autohersteller autoindustrie autobranche branche group ag se
""".split())

_SUFFIXES = ("ungen", "heiten", "keiten", "ern", "ung", "heit", "keit", "en", "er", "es", "em", "e", "n", "s")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def fold(text: str) -> str:
    text = text.lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def stem(word: str) -> str:
    if len(word) <= 4 or word[0].isdigit():
        return word
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def tokens(text: str) -> list[str]:
    out = []
    for raw in _WORD.findall(text or ""):
        w = fold(raw)
        if len(w) < 3 and not w.isdigit():
            continue
        if w in STOPWORDS or w in GENERIC:
            continue
        out.append(stem(w))
    return out


def title_key(title: str) -> str:
    """Normalisierter Titel für die Dublettenerkennung."""
    t = fold(title)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()[:200]


_TRACKING = re.compile(r"^(utm_|wt_|wt\.|ref$|cid$|xtor$|fbclid$|gclid$|r$|ns_)", re.I)


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(q), ""))


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, domain: str) -> bool:
    host = domain_of(url)
    return host == domain or host.endswith("." + domain)


def shorten(text: str, max_chars: int = 420) -> str:
    text = normalize_ws(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # am Satzende kürzen, wenn möglich
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m > max_chars * 0.5:
        return cut[: m + 1]
    return cut.rsplit(" ", 1)[0] + " …"


_CJK_CHAR = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_EN_WORDS = set("the and of to in for is on with by at from as its it that will has have says said after over "
                "new than more amid sales cars car".split())
_DE_WORDS = set("der die das und den von mit für ist im des dem nicht auf eine ein sich auch bei zu".split())


def detect_lang(text: str, default: str = "de") -> str:
    """Grobe Spracherkennung (de/en/zh) für Titel und Teaser."""
    if not text:
        return default
    cjk = len(_CJK_CHAR.findall(text))
    if cjk >= 2 and cjk >= 0.2 * len(text.replace(" ", "")):
        return "zh"
    words = re.findall(r"[a-zäöüß]+", text.lower())
    en = sum(w in _EN_WORDS for w in words)
    de = sum(w in _DE_WORDS for w in words) + sum(ch in "äöüß" for ch in text.lower())
    if en >= 2 and en > de:
        return "en"
    if de >= 2 and de > en:
        return "de"
    return default


def cjk_bigrams(text: str) -> list[str]:
    """Für chinesische Texte: überlappende Zeichenpaare als Tokens (Themenbildung)."""
    out = []
    for run in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]+", text or ""):
        out += [run[i:i + 2] for i in range(max(1, len(run) - 1))]
    return out
