"""Referenzfälle für die regelbasierte Tonalität (positiv / neutral / negativ).

Die Fälle spiegeln die fachlichen Vorgaben wider:
positiv = vorteilhaft bzw. nicht von Nachteil (neue Produkte, Verbesserungen, Kundenzuspruch, gute Tests,
gute Marktentwicklung, bessere Finanzergebnisse, Ansehen); negativ = Kritik, Rückrufe, durchschnittliche
oder schlechte Tests, rückläufige Zahlen, Verzögerungen, Ansehensschaden; neutral = nicht eindeutig.
"""
import pytest

from app import sentiment as s

CASES = [
    # Neue Produkte, Verbesserungen
    ("VW stellt neuen ID.2 vor", "positiv"),
    ("Weltpremiere: Škoda enthüllt den Elroq", "positiv"),
    ("Neuer Golf GTI: Mehr Leistung, verbesserte Software", "positiv"),
    ("Marktstart für den Cupra Raval", "positiv"),
    ("Update verbessert Reichweite des ID.4", "positiv"),
    # Kundenzuspruch
    ("ID.7 wird zum Bestseller in Europa", "positiv"),
    ("Großauftrag für MAN: 500 Busse für Hamburg", "positiv"),
    # Testergebnisse – durchschnittlich gilt als negativ
    ("ID.7 ist Testsieger im Vergleich", "positiv"),
    ("Fünf Sterne im Euro NCAP für den Passat", "positiv"),
    ("Golf im ADAC-Test nur befriedigend", "negativ"),
    ("Nur drei Sterne im Euro NCAP für den Tiguan", "negativ"),
    ("Polo schneidet im Test durchschnittlich ab", "negativ"),
    ("Tiguan fällt im Elchtest durch", "negativ"),
    # Kennzahl + Richtung
    ("Volkswagen: Absatz in China sinkt deutlich", "negativ"),
    ("Umsatz von Audi steigt um zehn Prozent", "positiv"),
    ("Porsche: Verlust sinkt im dritten Quartal", "positiv"),
    ("Verluste steigen bei Cariad", "negativ"),
    ("Marge schrumpft auf 3,5 Prozent", "negativ"),
    ("Auftragseingang bei MAN legt zu", "positiv"),
    ("Aktie von Porsche bricht ein", "negativ"),
    ("Absatzplus in Europa für VW", "positiv"),
    ("Umsatzrückgang bei Porsche", "negativ"),
    ("Volkswagen steigert den Gewinn deutlich", "positiv"),
    # Finanzen / Prognose
    ("Porsche hebt Prognose an", "positiv"),
    ("Gewinnwarnung bei Volkswagen", "negativ"),
    ("Quartalszahlen besser als erwartet", "positiv"),
    # Rückrufe, Kritik, Verzögerungen, Ansehen
    ("Rückruf: VW ruft 100.000 Autos wegen Airbag zurück", "negativ"),
    ("Start des ID.2 verzögert sich um ein Jahr", "negativ"),
    ("Kritik an VW-Software wächst", "negativ"),
    ("Razzia bei Audi wegen Abgasskandal", "negativ"),
    ("Vorwürfe wegen Zwangsarbeit in Xinjiang", "negativ"),
    ("VW streicht 35.000 Stellen", "negativ"),
    # Verneinung / Einräumung
    ("Kein Stellenabbau bei VW in Zwickau", "positiv"),
    ("Trotz Krise: VW meldet Rekordabsatz", "positiv"),
    # Neutral: sachlich oder gemischt
    ("VW-Chef Blume besucht IAA in München", "neutral"),
    ("Volkswagen ernennt neuen Finanzvorstand", "neutral"),
    ("Die Übergabe erfolgt morgen in Wolfsburg", "neutral"),
    ("Absatz steigt, Marge sinkt", "neutral"),
    ("Volkswagen mit Licht und Schatten: Rekordumsatz, aber Gewinnwarnung", "neutral"),
    # „aber“ betont den Stellenabbau – überwiegend negativ
    ("VW investiert in Werk, streicht aber 2.000 Stellen", "negativ"),
    # Fallen von früher
    ("VW-Werk in Brandenburg schließlich eröffnet", "positiv"),
    ("Tarifeinigung bei VW: Beschäftigungssicherung bis 2030", "positiv"),
]


@pytest.mark.parametrize("title,expected", CASES)
def test_reference_cases(title, expected):
    r = s.analyze(title)
    assert s.classify(r.pos, r.neg) == expected, (title, r.detail())


def test_title_counts_double_and_detail():
    r = s.analyze("Rückruf bei Audi", "Der Hersteller hat eine neue Plattform vorgestellt.")
    assert r.neg > r.pos and s.classify(r.pos, r.neg) == "negativ"
    d = r.detail()
    assert d["hits"][0]["k"] == "Rückruf" and d["hits"][0]["w"] == -6.0


def test_classify_thresholds():
    assert s.classify(0, 0) == "neutral"
    assert s.classify(1.0, 0) == "neutral"            # unter der Marge
    assert s.classify(4, 0) == "positiv"
    assert s.classify(0, 4) == "negativ"
    assert s.classify(6, 5) == "neutral"              # stark gemischt
    assert s.classify(6, 2) == "positiv"
    assert s.classify(3, 0, margin=4) == "neutral"    # strengere Marge
