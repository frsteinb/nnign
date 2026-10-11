# Bericht: nnign (No News Is Good News) – Stand 10.10.2026

## Kurzfassung

Das System ist fertig gebaut, getestet und als Docker-Image lauffähig (`docker compose up -d --build`,
dann <http://localhost:8000>). Es sammelt Meldungen über den VW-Konzern aus 15 seriösen deutschen
Medien, fasst sie zu Themen zusammen, bewertet jedes Thema als positiv oder negativ und speichert
Datum, Kurzthema, Abstract, 1–5 Quellen und das erste Erscheinungsdatum. Auf Ihren Zusatzwunsch hin
geht der erste Lauf **bis ins Jahr 2000 zurück** und zeigt die Entwicklung als Zeitstrahl.

**Wichtigste Einschränkung:** In meiner Entwicklungsumgebung waren alle externen Websites gesperrt
(Nachrichtenseiten, Google News, Internet Archive). Ich konnte deshalb **nicht live gegen die echten
Quellen testen.** Alle Abläufe sind gegen einen lokalen Mock-Server mit realistischen Feeds,
Archivseiten, Suchtreffern und Wayback-Antworten getestet. Die konkreten Feed-URLs und Archiv-Vorlagen
stammen aus meinem Wissen über die Seiten und sind **unverifiziert**. Das System zeigt aber genau an,
welche Quelle funktioniert, und alles lässt sich ohne Code-Änderung im Admin-Bereich korrigieren
(siehe „Erster Start – worauf achten“).

## Was gebaut wurde

| Anforderung | Umsetzung |
|---|---|
| Docker-Image, Webserver auf Port 8000 | `Dockerfile` (python:3.12-slim, läuft ohne Root, mit Healthcheck) + `docker-compose.yml` mit Datenvolume `./data` |
| Initial sehr umfänglich | Initial-Scan: alle Feeds + Nachrichtensuche der letzten 60 Tage in Wochenfenstern. Danach **historischer Backfill bis 2000** über vier Wege (siehe unten) |
| Danach im Intervall (Standard 4 h) | Zeitsteuerung im Hintergrund; Intervall im Admin-Bereich änderbar; Button „Jetzt scannen“ |
| Gängige deutsche seriöse Quellen | tagesschau.de, SPIEGEL, ZEIT, FAZ, SZ, Handelsblatt, WirtschaftsWoche, manager magazin, n-tv, Deutschlandfunk, NDR Niedersachsen, Tagesspiegel, WELT, heise, Automobilwoche – im Admin-Bereich erweiterbar |
| Nur **neue** Nachrichten | Dubletten werden über normalisierte URL (ohne Tracking-Parameter) und normalisierten Titel je Medium erkannt; bereits bekannte Meldungen werden nie erneut verarbeitet |
| Themenbildung | Meldungen derselben Geschichte (auch aus verschiedenen Medien) werden über TF-IDF-Kosinus-Ähnlichkeit in einem Zeitfenster von ±6 Tagen zu einem Thema zusammengefasst; mit KI entscheidet Claude |
| Positiv/negativ | Regelbasiert über ein deutsches Wirtschaftslexikon (≈200 gewichtete Begriffe, Verneinungen) oder per KI; der Admin kann jede Bewertung von Hand korrigieren |
| Gespeicherte Felder | Erfassungsdatum, Kurzthema, Abstract, Bewertung (+ Score und Herkunft: lexikon/llm/manuell), erstes und letztes Datum, alle Meldungen; 1–5 Quellen werden angezeigt und exportiert |
| Später konfigurierbar | 37 Einstellungen im Admin-Bereich (Intervall, Zeitraum, Schlagwörter, Schwellen, KI, Abrufpausen …), Quellen anlegen/ändern/deaktivieren, Themen bearbeiten/zusammenführen/abtrennen/löschen |
| Sofort lauffähig | Keine Pflicht-Konfiguration. Admin-Passwort wird erzeugt und im Log ausgegeben; ohne API-Key läuft alles regelbasiert |

Zusätzlich: JSON-API (`/api/...`, Doku unter `/docs`), CSV-Export, Zeitstrahl positiv/negativ je Jahr
(Klick zeigt die Monate), Hell-/Dunkelmodus, Handy-tauglich.

## Der Weg zurück bis 2000

RSS-Feeds reichen nur Tage zurück. Für die Historie nutzt das System vier Wege:

1. **Archivseiten der Medien:** SPIEGEL-Nachrichtenarchiv (tägliche Seiten ab 2000), tagesschau-Archiv
   (täglich ab 2007), ZEIT-Ausgaben (wöchentlich ab 2000), heise-Newsticker-Archiv (monatlich ab 2000).
   Für relevante Treffer wird die Artikelseite geladen, um Teaser und genaues Datum zu lesen.
2. **Nachrichtensuche** (Google News RSS) mit `site:domain` in 90-Tage-Fenstern für alle 15 Medien.
3. **Internet Archive:** monatliche archivierte Schnappschüsse aller Feeds (ab ca. 2003–2005, je nach
   Medium). Das erschließt auch FAZ, SZ, Handelsblatt usw., die kein frei zugängliches Archiv haben.
4. **Ersatzweg:** Ist eine Archivseite direkt nicht erreichbar, wird der Wayback-Schnappschuss geladen.

Das sind beim ersten Start rund **20.000 Abrufaufgaben**. Bei höflichen Abrufpausen (1,2 s je Host,
4 s beim Internet Archive) rechne ich mit **10–15 Stunden**, bis die Historie vollständig ist. Damit
nicht 15 Stunden lang nur das Jahr 2000 zu sehen ist, läuft der Backfill **grob nach fein**: zuerst
jeder 32. Tag über den ganzen Zeitraum, dann immer dichter. Nach 1–2 Stunden gibt es so einen ersten
Überblick über alle Jahre. Der Fortschritt steht auf der Startseite und je Quelle im Admin-Bereich.
Der Backfill übersteht Neustarts, weil jede Aufgabe in der Datenbank abgehakt wird.

## Getestet

* 13 automatisierte Tests (`pytest`), komplett offline gegen einen lokalen Mock-Server, darunter:
  * Initial-Scan: zwei Medien berichten über den Stellenabbau → **ein** Thema mit zwei Quellen, negativ,
    erstes Datum = früheste Meldung.
  * Ausschlüsse: VfL-Wolfsburg-Fußball, BMW-Meldung und Suchtreffer fremder Medien werden verworfen.
  * Zweiter Scan mit denselben Feeds: **0 neue** Meldungen und Themen.
  * Backfill: Archivseite → Artikeldetails (Teaser/Datum aus Meta-Tags) → Thema; eine Wayback-Feedmeldung
    zur selben Geschichte landet im selben Thema; „Winterkorn tritt zurück“ wird korrekt auf 2015 datiert.
  * Abbruchsicherung: eine Archivquelle mit falscher URL-Vorlage wird automatisch gestoppt.
  * KI-Pfad mit nachgebildetem Claude-Client (Format der Anfrage, „nicht relevant“, Themenzuordnung,
    Tageslimit, Ablehnungsfall). Ein echter API-Aufruf war mangels Key nicht möglich.
  * Web, API, CSV, Admin-Login, Einstellungen speichern, CSRF-Schutz, Themenbearbeitung, Login-Pflicht
    bei nicht öffentlichem Dashboard.
* Docker-Image gebaut und gestartet: Healthcheck grün, Passwort-Ausgabe im Log, ohne Netz korrekte
  Meldung „keine Quelle erreichbar“ und automatische Wiederholung alle 10 Minuten.
* Oberfläche mit 214 Demo-Themen (2000–2026) in Hell, Dunkel und Handybreite geprüft (Screenshots).

### Nachtrag 1.2.0 – fremdsprachige Quellen und Updates

- Neue Quellen: Braunschweiger Zeitung (de), Reuters, Bloomberg, Caixin Global, CnEVPost, Automotive News (en).
  Feeds nur für Bloomberg und CnEVPost hinterlegt (ungeprüft), sonst Nachrichtensuche `site:` in der englischen
  Google-News-Ausgabe.
- Übersetzung ins Deutsche über LibreTranslate (optionaler Container, ohne Key), DeepL oder KI; Original bleibt
  erhalten. Ohne Übersetzer: Originaltext plus englische/chinesische Bewertungsregeln.
- Abgleich beim Neustart nach Image-Update: neue Quellen nachholen, Backfill einplanen, Neu-Bewertung bei
  geänderten Regeln, Nachübersetzung. Getestet mit Mock-Servern (LibreTranslate, DeepL, OpenAI-kompatibel);
  echter LibreTranslate-Container in der Sandbox nicht startbar (kein Netz) – Sprachcode für Chinesisch
  (`zh` bzw. `zh-Hans`) wird daher automatisch durchprobiert.

### Nachtrag 1.4.0 – bessere Themenbündelung

- Neues Begriffsprofil (`app/features.py`): Synonyme und Wendungen, Komposita-Zerlegung, Modellnamen und
  Kennzahlen, Marken; Zuordnung über ähnlichste Einzelmeldung + Themenprofil, Abschläge bei widersprüchlichen
  Modellen/Marken; Zusammenführen über verbindende Meldungen; Höchstdauer eines Themas gegen Kettenbildung.
- Messung auf Referenzdaten (`python -m tests.cluster_eval`, 64 Meldungen/18 Geschichten): Trefferquote von
  29 % auf 93 %, Präzision 100 %; zurückgehaltener IAA-Satz (5 gleichzeitige Vorstellungen) fehlerfrei.
  Die Daten sind konstruiert, nicht aus echten Feeds – echte Fehlfälle bitte in FRAGEN.md H2 melden.
- Modellnamen (ID.*, Tiguan, Taycan …) und Konzernmanager gelten jetzt als Konzernbezug (Relevanz).
- Bestehende Daten werden nach dem Update beim Start automatisch neu gebündelt.

## Nicht getestet / Risiken

| Risiko | Auswirkung | Gegenmaßnahme im System |
|---|---|---|
| Feed-URLs oder Archiv-Vorlagen haben sich geändert | Quelle liefert nichts | Status je Quelle im Admin-Bereich; Archivquellen stoppen nach 15 Fehlschlägen automatisch; URL im UI korrigieren → „Backfill (neu) planen“ → „Fehlgeschlagene Aufgaben wiederholen“ |
| Google News drosselt viele automatisierte Suchanfragen | Weniger Suchtreffer | Feeds, Archivseiten und Wayback funktionieren unabhängig davon; Suche abschaltbar; Pausen einstellbar |
| tagesschau.de hat 2010 große Teile seines Archivs depubliziert (Rundfunkstaatsvertrag) | Lücken 2007–2010 | Wayback-Ersatzweg |
| Bezahlschranken (FAZ, Handelsblatt, SZ, Spiegel+) | Teaser fehlen teils | Titel und Teaser aus Feed/Meta-Tags reichen für Relevanz und Bewertung |
| Lexikon-Bewertung ist grob (z. B. Ironie, gemischte Meldungen) | Fehlbewertungen | KI-Modus; manuelle Korrektur sperrt das Thema gegen Überschreiben |
| Themenbildung ohne KI ist wortbasiert | Gleiche Geschichte in sehr unterschiedlicher Wortwahl wird evtl. getrennt | Schwelle einstellbar; „Zusammenführen“ und „Abtrennen“ im UI; KI-Modus |
| KI-Kosten | Mit Claude Opus 5.5 grob 0,5–1 Cent je neuer Meldung; im laufenden Betrieb (≈20–60 Meldungen/Tag) wenige Euro im Monat | Tageslimit (Standard 1.500 Aufrufe); Historie standardmäßig ohne KI; Wechsel auf `claude-haiku-5-5` senkt die Kosten etwa um den Faktor 40 |

## Getroffene Annahmen

* „VW-Konzern“ umfasst alle Konzernmarken und -töchter (Audi, Porsche, Škoda, Seat/Cupra, Bentley,
  Lamborghini, Bugatti, Ducati, Traton/MAN/Scania, Cariad, PowerCo, Moia) sowie die Porsche SE.
  Fußball (VfL Wolfsburg) und Arenen sind ausgeschlossen.
* „Positiv/negativ“ bedeutet: aus Sicht des Konzerns (wirtschaftliche Lage, Reputation, Rechtsrisiken,
  Beschäftigung). Es gibt bewusst kein „neutral“; bei Gleichstand gilt einstellbar „negativ“.
* „Datum“ = wann das System das Thema erfasst hat; „erstes Datum“ = früheste Veröffentlichung einer Quelle.
* Ein Thema ist eine konkrete Nachrichtengeschichte (z. B. „Werksschließungen in Deutschland angekündigt“),
  kein Dauer-Oberthema (z. B. „Dieselskandal“). Neue Entwicklungen nach mehr als 6 Tagen werden zu neuen
  Themen – so wird die Entwicklung über die Zeit sichtbar.
* Das Dashboard ist ohne Login lesbar; nur Änderungen sind passwortgeschützt (umstellbar).
* Ohne API-Key soll das System trotzdem voll funktionieren; die KI ist eine optionale Verbesserung.
* Volltexte werden nicht gespeichert (Urheberrecht), nur Titel, Teaser und Links.

## Erster Start – worauf achten

1. `docker compose up -d --build`, dann `docker logs nnign` für das Admin-Passwort.
2. Nach ca. 2–5 Minuten ist der Initial-Scan fertig. Unter **/admin → Quellen** sehen Sie je Medium
   „ok“ oder die Fehlermeldung. Bei „HTTP 404“ ist die Feed-URL veraltet: aktuelle URL eintragen.
3. Unter **/admin → Historischer Backfill je Quelle** nach etwa einer Stunde prüfen: Steht bei einer
   Archivquelle „gestoppt“, ist die URL-Vorlage oder das Link-Muster falsch. Das Fehlerprotokoll darunter
   zeigt den letzten Fehler.
4. Optional API-Key eintragen und „KI-Verbindung testen“.

Ich empfehle, danach die Fragen in [FRAGEN.md](FRAGEN.md) durchzugehen. Die Antworten sind die
Grundlage für die nächste Ausbaustufe.

## Mögliche nächste Schritte

* Erste echte Ergebnisse sichten und Schwelle/Lexikon anhand von 50–100 bewerteten Themen kalibrieren.
* Dauerthemen („Storylines“, z. B. Dieselskandal 2015–2026) als zweite Ebene über den Einzelthemen.
* Benachrichtigung (E-Mail/Teams/Slack) bei neuen stark negativen Themen.
* Feinere Bewertung (Stärke 1–5, betroffene Marke, Kategorie wie Finanzen/Recht/Produkt/Personal).
* Weitere Quellen (Regionalpresse Wolfsburg/Braunschweig, Börsen-Zeitung, Fachpresse, Pressemitteilungen des Konzerns).
* Einmaliger KI-Durchlauf über die Historie, um historische Themen sauberer zu bündeln und zu bewerten.
