# VW-Nachrichtenmonitor

Durchsucht seriöse deutschsprachige Nachrichtenquellen nach Meldungen über den Volkswagen-Konzern,
fasst sie zu **Nachrichtenthemen** zusammen, bewertet jedes Thema als **positiv** oder **negativ** und
stellt alles über einen Webserver auf **Port 8000** bereit.

* **Beim ersten Start:** umfassender Initial-Scan (aktuelle Feeds + Nachrichtensuche der letzten 60 Tage),
  danach ein **historischer Backfill bis zum Jahr 2000**, der im Hintergrund läuft.
* **Danach:** alle **4 Stunden** (einstellbar) ein Scan auf *neue* Meldungen.
* Pro Thema gespeichert: Erfassungsdatum, Kurzthema, Abstract, Bewertung, 1–5 Quellen (Links), erstes Datum.
* Alles ist im Admin-Bereich unter `/admin` konfigurierbar.

## Schnellstart

```bash
docker compose up -d --build
```

Dann <http://localhost:8000> öffnen. Ohne Docker Compose:

```bash
docker build -t vw-nachrichtenmonitor .
docker run -d --name vw-news -p 8000:8000 -v "$PWD/data:/data" vw-nachrichtenmonitor
```

**Admin-Zugang:** Benutzer `admin`. Ist `ADMIN_PASSWORD` nicht gesetzt, wird beim ersten Start ein
Passwort erzeugt und im Log ausgegeben (`docker logs vw-nachrichtenmonitor`) sowie in
`data/admin_password.txt` abgelegt. Es kann unter `/admin` geändert werden.

**Optional – KI-Analyse:** Mit `ANTHROPIC_API_KEY` (Umgebungsvariable oder im Admin-Bereich) bewertet
Claude Relevanz, Themenzuordnung, Kurztitel, Abstract und Tonalität. Ohne Key arbeitet das System
vollständig regelbasiert.

```bash
ADMIN_PASSWORD=meinPasswort ANTHROPIC_API_KEY=sk-ant-... docker compose up -d --build
```

## Seiten und Schnittstellen

| Pfad | Inhalt |
|---|---|
| `/` | Themenliste mit Filtern (Suche, Bewertung, Medium, Zeitraum) und Zeitstrahl positiv/negativ seit 2000; Klick auf ein Jahr zeigt die Monate |
| `/thema/{id}` | Thema mit allen Meldungen; Bearbeiten, Zusammenführen, Abtrennen, Löschen (Admin) |
| `/admin` | Status, Backfill-Fortschritt je Quelle, Einstellungen, Quellenverwaltung, Scan-Protokolle |
| `/api/topics`, `/api/topics/{id}`, `/api/timeline`, `/api/status` | JSON-API (gleiche Filterparameter wie `/`) |
| `/export.csv` | CSV-Export (Semikolon, UTF-8, Excel-tauglich) |
| `/docs` | automatisch erzeugte API-Dokumentation |
| `/healthz` | Health-Check |

## Konfiguration

Jede Einstellung hat einen eingebauten Standardwert, kann per Umgebungsvariable `NNIGN_<NAME>`
vorbelegt werden (z. B. `NNIGN_SCAN_INTERVAL_HOURS=2`, `NNIGN_BACKFILL_SINCE=2005-01-01`) und im
Admin-Bereich überschrieben werden (der Admin-Wert hat Vorrang). Die wichtigsten:

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `scan_interval_hours` | 4 | Intervall der regulären Scans |
| `backfill_since` | 2000-01-01 | Wie weit der historische Backfill zurückgeht |
| `search_enabled` | an | Nachrichtensuche (Google News RSS mit `site:`) zusätzlich zu den Feeds |
| `keywords_strong` / `keywords_brands` / `keywords_exclude` | siehe UI | Relevanzregeln (reguläre Ausdrücke) |
| `cluster_threshold` / `cluster_window_days` | 0,34 / 6 | Wann Meldungen zum selben Thema zusammengefasst werden |
| `llm_model` | claude-opus-5-5 | Modell für die KI-Analyse (z. B. `claude-haiku-5-5` für deutlich geringere Kosten) |
| `llm_for_backfill` | aus | KI auch für die Historie verwenden (viele tausend Aufrufe) |
| `public_dashboard` | an | Übersicht ohne Login sichtbar |

Weitere Umgebungsvariablen: `ADMIN_PASSWORD`, `ANTHROPIC_API_KEY`, `DATA_DIR` (Standard `/data`),
`LOG_LEVEL`.

## Woher die Meldungen kommen

| Weg | Zweck |
|---|---|
| RSS/Atom-Feeds der Medien | aktuelle Meldungen, mit Teaser |
| Nachrichtensuche (`site:domain`, Zeitfenster) | findet auch Artikel, die nicht (mehr) im Feed stehen; im Backfill in 90-Tage-Fenstern bis 2000 |
| Archivseiten der Medien (Spiegel täglich, tagesschau täglich, ZEIT wöchentlich, heise monatlich) | lückenlose Historie; für Treffer werden Teaser und Datum aus den Meta-Tags der Artikelseite gelesen |
| Internet Archive (Wayback Machine) | archivierte Schnappschüsse der Feeds (monatlich), auch für Medien ohne eigenes Archiv; außerdem Ersatz, wenn eine Archivseite nicht erreichbar ist |

Der Backfill arbeitet **grob nach fein**: zuerst jede 32. Periode über den ganzen Zeitraum, dann
immer dichter. So steht schon nach kurzer Zeit ein Überblick über alle Jahre, der sich laufend
verfeinert. Archivquellen, die dauerhaft nichts liefern (z. B. weil sich eine URL geändert hat),
werden nach 15 Fehlschlägen in Folge automatisch gestoppt und im Admin-Bereich angezeigt.

## Daten

Alles liegt in einer SQLite-Datei unter `./data/vw_news.sqlite3` (Volume `/data`). Sichern = Datei
kopieren. Zurücksetzen = Container stoppen, Datei löschen, starten.

## Entwicklung

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest                       # Tests laufen komplett offline gegen einen lokalen Mock-Server
uvicorn app.main:app --reload --port 8000
```

Aufbau: `app/fetcher.py` (Abruf), `app/relevance.py` (Relevanz), `app/sentiment.py` (Lexikon),
`app/topics.py` (Themenbildung, TF-IDF-Kosinus), `app/llm.py` (Claude), `app/pipeline.py`
(Scans, Backfill, Zeitsteuerung), `app/main.py` (Web/API/Admin).

Siehe auch [BERICHT.md](BERICHT.md) und [FRAGEN.md](FRAGEN.md).
